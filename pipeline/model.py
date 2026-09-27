"""Stage 3 - Transform/model. Load a small star schema into SQLite.

  dim_zone   (zone_id)        - TLC zone lookup + our is_crz business attribute
  dim_date   (date_key)       - calendar + holidays (API) + weather (API) + policy period
  fact_trip  (trip_id)        - one row per trip record, ALL rows kept, with dq/warn flags

Rerun behaviour: each month is a partition. Loading a month deletes that month's rows
and re-inserts them inside one transaction, so a rerun never duplicates data and a
crash mid-load leaves the previous version intact.
"""
import json
import logging
import sqlite3

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS dim_zone (
    zone_id      INTEGER PRIMARY KEY,
    borough      TEXT,
    zone         TEXT,
    service_zone TEXT,
    is_crz       INTEGER NOT NULL,   -- 1 = inside Congestion Relief Zone (assumed list, validated)
    is_unknown   INTEGER NOT NULL    -- 264/265
);
CREATE TABLE IF NOT EXISTS dim_date (
    date_key     INTEGER PRIMARY KEY, -- yyyymmdd
    date         TEXT NOT NULL,
    year_month   TEXT NOT NULL,
    day_of_week  INTEGER NOT NULL,    -- 0 = Monday
    is_weekend   INTEGER NOT NULL,
    is_holiday   INTEGER NOT NULL,
    holiday_name TEXT,
    precip_mm    REAL,
    snowfall_cm  REAL,
    temp_mean_c  REAL,
    is_wet_day   INTEGER,
    is_toll_live INTEGER NOT NULL     -- 1 = CRZ toll in force (>= 2025-01-05)
);
CREATE TABLE IF NOT EXISTS fact_trip (
    trip_id       INTEGER PRIMARY KEY, -- yyyymm * 10^8 + row number in source file (traceable to raw)
    year_month    TEXT NOT NULL,
    vendor_id     INTEGER,
    pickup_ts     TEXT NOT NULL,
    date_key      INTEGER NOT NULL REFERENCES dim_date(date_key),
    pickup_hour   INTEGER NOT NULL,
    pu_zone_id    INTEGER REFERENCES dim_zone(zone_id),
    do_zone_id    INTEGER REFERENCES dim_zone(zone_id),
    segment       TEXT NOT NULL,      -- crz_internal / crz_inbound / crz_outbound / manhattan_non_crz / other
    payment_type  INTEGER,
    distance_mi   REAL,
    duration_s    REAL,
    speed_mph     REAL,
    fare_amount   REAL,
    tip_amount    REAL,
    cbd_fee       REAL,               -- NULL before the source published the column
    dq_flags      INTEGER NOT NULL,
    warn_flags    INTEGER NOT NULL,
    is_valid      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_fact_month_segment ON fact_trip (year_month, segment, is_valid);
CREATE TABLE IF NOT EXISTS metrics_monthly (
    year_month  TEXT NOT NULL,
    metric      TEXT NOT NULL,
    value       REAL,
    n_trips     INTEGER,
    computed_at TEXT NOT NULL,
    PRIMARY KEY (year_month, metric)
);
"""


def connect() -> sqlite3.Connection:
    config.WAREHOUSE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.WAREHOUSE_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def build_dim_zone(zone_csv) -> pd.DataFrame:
    z = pd.read_csv(zone_csv)
    return pd.DataFrame({
        "zone_id": z["LocationID"],
        "borough": z["Borough"],
        "zone": z["Zone"],
        "service_zone": z["service_zone"],
        "is_crz": z["LocationID"].isin(config.CRZ_ZONE_IDS).astype(int),
        "is_unknown": z["LocationID"].isin(config.UNKNOWN_ZONE_IDS).astype(int),
    })


def build_dim_date(month: str, weather_json, holidays_json) -> pd.DataFrame:
    w = json.loads(open(weather_json).read())["daily"]
    days = pd.to_datetime(w["time"])
    # Only nationwide holidays affect NYC traffic patterns; state-only entries are ignored.
    hol = {h["date"]: h["name"] for h in json.loads(open(holidays_json).read()) if h.get("global")}
    d = pd.DataFrame({"date": days.strftime("%Y-%m-%d")})
    d["date_key"] = days.strftime("%Y%m%d").astype(int)
    d["year_month"] = month
    d["day_of_week"] = days.dayofweek
    d["is_weekend"] = (days.dayofweek >= 5).astype(int)
    d["holiday_name"] = d["date"].map(hol)
    d["is_holiday"] = d["holiday_name"].notna().astype(int)
    d["precip_mm"] = w["precipitation_sum"]
    d["snowfall_cm"] = w["snowfall_sum"]
    d["temp_mean_c"] = w["temperature_2m_mean"]
    d["is_wet_day"] = (d["precip_mm"].fillna(0) >= config.DAYS_WITH_PRECIP_MM).astype(int)
    d["is_toll_live"] = (d["date"] >= config.CRZ_START_DATE).astype(int)
    return d[["date_key", "date", "year_month", "day_of_week", "is_weekend", "is_holiday",
              "holiday_name", "precip_mm", "snowfall_cm", "temp_mean_c", "is_wet_day", "is_toll_live"]]


def assign_segment(pu: pd.Series, do: pd.Series, zones: pd.DataFrame) -> np.ndarray:
    crz = set(zones.loc[zones["is_crz"] == 1, "zone_id"])
    manhattan = set(zones.loc[zones["borough"] == "Manhattan", "zone_id"])
    pu_crz, do_crz = pu.isin(crz), do.isin(crz)
    pu_mn_other = pu.isin(manhattan - crz)
    do_mn_other = do.isin(manhattan - crz)
    return np.select(
        [pu_crz & do_crz, ~pu_crz & do_crz, pu_crz & ~do_crz, pu_mn_other & do_mn_other],
        ["crz_internal", "crz_inbound", "crz_outbound", "manhattan_non_crz"],
        default="other",
    )


def build_fact(df: pd.DataFrame, month: str, zones: pd.DataFrame) -> pd.DataFrame:
    pu = df["tpep_pickup_datetime"]
    out = pd.DataFrame({
        "trip_id": int(month.replace("-", "")) * 10**8 + np.arange(len(df), dtype=np.int64),
        "year_month": month,
        "vendor_id": df["VendorID"],
        "pickup_ts": pu.dt.strftime("%Y-%m-%d %H:%M:%S"),
        "date_key": pu.dt.strftime("%Y%m%d").astype(int),
        "pickup_hour": pu.dt.hour,
        "pu_zone_id": df["PULocationID"],
        "do_zone_id": df["DOLocationID"],
        "segment": assign_segment(df["PULocationID"], df["DOLocationID"], zones),
        "payment_type": df["payment_type"],
        "distance_mi": df["trip_distance"],
        "duration_s": df["duration_s"],
        "speed_mph": df["speed_mph"].round(3),
        "fare_amount": df["fare_amount"],
        "tip_amount": df["tip_amount"],
        "cbd_fee": df["cbd_congestion_fee"] if "cbd_congestion_fee" in df else np.nan,
        "dq_flags": df["dq_flags"],
        "warn_flags": df["warn_flags"],
        "is_valid": df["is_valid"],
    })
    return out


def load_month(conn: sqlite3.Connection, month: str, trips: pd.DataFrame, raw: dict) -> int:
    zones = build_dim_zone(raw["zones"])
    dates = build_dim_date(month, raw["weather"], raw["holidays"])
    fact = build_fact(trips, month, zones)
    # Out-of-period rows have pickup dates outside dim_date; they are stored against a
    # sentinel date so the FK holds and the row stays auditable.
    known = set(dates["date_key"])
    fact.loc[~fact["date_key"].isin(known), "date_key"] = 19000101

    with conn:  # single transaction: all-or-nothing
        # Dimensions are upserted, never deleted: other months' facts still reference them.
        conn.executemany("INSERT INTO dim_zone VALUES (?,?,?,?,?,?) ON CONFLICT(zone_id) DO UPDATE SET "
                         "borough=excluded.borough, zone=excluded.zone, service_zone=excluded.service_zone, "
                         "is_crz=excluded.is_crz, is_unknown=excluded.is_unknown",
                         zones.itertuples(index=False, name=None))
        conn.execute("INSERT OR IGNORE INTO dim_date (date_key, date, year_month, day_of_week, "
                     "is_weekend, is_holiday, is_toll_live) VALUES (19000101,'1900-01-01','n/a',0,0,0,0)")
        conn.execute("DELETE FROM fact_trip WHERE year_month = ?", (month,))
        upd = ", ".join(f"{c}=excluded.{c}" for c in dates.columns if c != "date_key")
        conn.executemany(f"INSERT INTO dim_date VALUES ({','.join('?' * dates.shape[1])}) "
                         f"ON CONFLICT(date_key) DO UPDATE SET {upd}",
                         dates.astype(object).where(dates.notna(), None).itertuples(index=False, name=None))
        cols = list(fact.columns)
        sql = f"INSERT INTO fact_trip ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
        for start in range(0, len(fact), 250_000):  # chunked to bound memory
            chunk = fact.iloc[start:start + 250_000]
            conn.executemany(sql, chunk.astype(object).where(chunk.notna(), None)
                             .itertuples(index=False, name=None))
    n = conn.execute("SELECT COUNT(*) FROM fact_trip WHERE year_month = ?", (month,)).fetchone()[0]
    if n != len(fact):
        from .errors import PipelineError
        raise PipelineError(f"{month}: warehouse holds {n} rows, expected {len(fact)}")
    log.info("Loaded %s: %d fact rows, %d dates, %d zones", month, n, len(dates), len(zones))
    return n
