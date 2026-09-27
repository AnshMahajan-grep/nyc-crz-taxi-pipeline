"""Stage 4 - Metrics, computed in SQL against the modelled warehouse.

KPI population ("CRZ peak weekday trips"): valid trips that start AND end inside the
Congestion Relief Zone, on non-holiday weekdays, picked up 05:00-20:59 (toll peak window).
Control population: identical filters, but trips wholly inside Manhattan north of the CRZ -
same city, same weather, same fleet, no toll - so we can separate the policy effect from a
general Jan-2024 vs Jan-2025 difference.
"""
import logging
import sqlite3
from datetime import datetime, timezone

import pandas as pd

from . import config

log = logging.getLogger(__name__)

HAS_REVERSAL = 8  # validate.WARN_BITS["HAS_REVERSAL"]
PAYMENT_TYPE_0 = 1  # validate.WARN_BITS["PAYMENT_TYPE_0"]
PEAK = f"f.pickup_hour BETWEEN {min(config.PEAK_WEEKDAY_HOURS)} AND {max(config.PEAK_WEEKDAY_HOURS)}"

POP = """
FROM fact_trip f JOIN dim_date d ON d.date_key = f.date_key
WHERE f.year_month = :m AND f.is_valid = 1
  AND d.date BETWEEN :d_from AND :d_to
  AND d.is_weekend = 0 AND d.is_holiday = 0 AND {peak}
  AND f.segment = '{segment}' {extra}
"""

# Metric catalogue: name -> (definition, business question). Order = evidence table order.
CATALOGUE = {
    "valid_trip_rate": ("Share of all trip records passing every exclude rule",
                        "Can leadership trust this month's numbers?"),
    "crz_median_speed_mph": ("PROJECT KPI. Median speed (distance/duration) of CRZ-internal trips, weekday peak",
                             "Did trips inside the zone get faster?"),
    "control_median_speed_mph": ("Same as KPI, for Manhattan trips wholly outside the CRZ (no toll)",
                                 "Would speeds have changed anyway?"),
    "crz_p90_duration_min": ("90th-percentile duration of CRZ-internal weekday-peak trips",
                             "Did the slow tail (reliability) improve?"),
    "crz_fare_per_engaged_hour": ("Sum of metered fare / sum of trip hours, CRZ-internal weekday peak, reversals excluded",
                                  "Do drivers earn more per hour on the meter in the zone?"),
    "crz_touching_trips_per_weekday": ("Valid trips starting and/or ending in the CRZ, per non-holiday weekday",
                                       "Did demand for zone trips fall because of the toll?"),
    "crz_median_speed_dry_days": ("Sensitivity: KPI restricted to days with < 1 mm precipitation",
                                  "Is the KPI change just weather?"),
    "control_median_speed_dry_days": ("Sensitivity: control-group median speed on days with < 1 mm precipitation",
                                      "Net of weather, is there still a CRZ effect?"),
    "crz_median_speed_excl_pt0": ("Sensitivity: KPI excluding payment_type 0 records",
                                  "Is the KPI change caused by the shift in payment_type 0 records?"),
}


def _pop(segment: str, extra: str = "") -> str:
    return POP.format(peak=PEAK, segment=segment, extra=extra)


def _percentile(conn, col: str, pop_sql: str, params: dict, p: float) -> tuple[float | None, int]:
    n = conn.execute(f"SELECT COUNT(*) {pop_sql}", params).fetchone()[0]
    if n == 0:
        return None, 0
    offset = int((n - 1) * p)
    row = conn.execute(f"SELECT {col} {pop_sql} ORDER BY {col} LIMIT 1 OFFSET {offset}", params).fetchone()
    return float(row[0]), n


def compute(conn: sqlite3.Connection, month: str, d_from: str, d_to: str) -> dict:
    """Return {metric: (value, n_trips)} for one month and date window."""
    p = {"m": month, "d_from": d_from, "d_to": d_to}
    out = {}

    total, valid = conn.execute(
        "SELECT COUNT(*), SUM(is_valid) FROM fact_trip WHERE year_month = :m", p).fetchone()
    out["valid_trip_rate"] = (valid / total if total else None, total)

    out["crz_median_speed_mph"] = _percentile(conn, "f.speed_mph", _pop("crz_internal"), p, 0.5)
    out["control_median_speed_mph"] = _percentile(conn, "f.speed_mph", _pop("manhattan_non_crz"), p, 0.5)
    v, n = _percentile(conn, "f.duration_s", _pop("crz_internal"), p, 0.9)
    out["crz_p90_duration_min"] = (v / 60 if v is not None else None, n)

    fare, hours, n = conn.execute(
        f"SELECT SUM(f.fare_amount), SUM(f.duration_s) / 3600.0, COUNT(*) "
        f"{_pop('crz_internal', f'AND (f.warn_flags & {HAS_REVERSAL}) = 0')}", p).fetchone()
    out["crz_fare_per_engaged_hour"] = (fare / hours if hours else None, n)

    trips, days = conn.execute("""
        SELECT COUNT(*), COUNT(DISTINCT f.date_key)
        FROM fact_trip f JOIN dim_date d ON d.date_key = f.date_key
        WHERE f.year_month = :m AND f.is_valid = 1 AND d.date BETWEEN :d_from AND :d_to
          AND d.is_weekend = 0 AND d.is_holiday = 0
          AND f.segment IN ('crz_internal', 'crz_inbound', 'crz_outbound')""", p).fetchone()
    out["crz_touching_trips_per_weekday"] = (trips / days if days else None, trips)

    out["crz_median_speed_dry_days"] = _percentile(
        conn, "f.speed_mph", _pop("crz_internal", "AND d.is_wet_day = 0"), p, 0.5)
    out["control_median_speed_dry_days"] = _percentile(
        conn, "f.speed_mph", _pop("manhattan_non_crz", "AND d.is_wet_day = 0"), p, 0.5)
    out["crz_median_speed_excl_pt0"] = _percentile(
        conn, "f.speed_mph", _pop("crz_internal", f"AND (f.warn_flags & {PAYMENT_TYPE_0}) = 0"), p, 0.5)
    return out


def month_bounds(month: str) -> tuple[str, str]:
    per = pd.Period(month)
    return per.start_time.strftime("%Y-%m-%d"), per.end_time.strftime("%Y-%m-%d")


def publish_monthly(conn: sqlite3.Connection, month: str) -> dict:
    """Full-month metrics -> metrics_monthly table (idempotent upsert)."""
    d_from, d_to = month_bounds(month)
    res = compute(conn, month, d_from, d_to)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        conn.execute("DELETE FROM metrics_monthly WHERE year_month = ?", (month,))
        conn.executemany("INSERT INTO metrics_monthly VALUES (?,?,?,?,?)",
                         [(month, k, v, n, now) for k, (v, n) in res.items()])
    log.info("Published %d metrics for %s", len(res), month)
    return res


def hourly_speed_profile(conn: sqlite3.Connection, month: str, d_from: str, d_to: str) -> pd.DataFrame:
    """Median CRZ-internal weekday speed by pickup hour (for the evidence chart)."""
    q = """
        SELECT f.pickup_hour AS hour, f.speed_mph
        FROM fact_trip f JOIN dim_date d ON d.date_key = f.date_key
        WHERE f.year_month = :m AND f.is_valid = 1 AND d.date BETWEEN :d_from AND :d_to
          AND d.is_weekend = 0 AND d.is_holiday = 0 AND f.segment = 'crz_internal'"""
    df = pd.read_sql_query(q, conn, params={"m": month, "d_from": d_from, "d_to": d_to})
    return df.groupby("hour")["speed_mph"].median().rename(month)
