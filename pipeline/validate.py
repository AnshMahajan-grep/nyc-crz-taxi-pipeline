"""Stage 2 - Profile & validate.

Principle: never silently fix or drop. Every rule sets a bit in a flag column, the row
stays in the warehouse, and the counts go into a validation report. Metrics then choose
which rows to use based on those flags.

  dq_flags   (EXCLUDE rules) - row is not a usable trip event for metrics
  warn_flags (WARN rules)    - row is usable but carries a known caveat

Quality gates are run-level checks; if any fails the month is NOT published.
"""
import json
import logging

import numpy as np
import pandas as pd

from . import config
from .errors import PipelineError

log = logging.getLogger(__name__)

# ---- EXCLUDE rules: (bit, code, business meaning)
EXCLUDE_RULES = [
    (1, "OUT_OF_PERIOD", "Pickup timestamp not in the file's month (clock errors, e.g. year 2002)"),
    (2, "NON_POSITIVE_DURATION", "Dropoff at or before pickup - timestamps cannot describe a trip"),
    (4, "TOO_SHORT", f"Duration < {config.MIN_DURATION_S}s - meter tapped on/off, not a ride"),
    (8, "TOO_LONG", f"Duration > {config.MAX_DURATION_S // 3600}h - meter left running"),
    (16, "ZERO_DISTANCE", "trip_distance <= 0 - no movement recorded"),
    (32, "IMPLAUSIBLE_DISTANCE", f"trip_distance > {config.MAX_DISTANCE_MI} mi - odometer/GPS error"),
    (64, "IMPLAUSIBLE_SPEED", f"Implied speed > {config.MAX_SPEED_MPH} mph"),
    (128, "NEGATIVE_FARE", "fare_amount < 0 - accounting reversal/adjustment record, not a trip"),
    (256, "UNKNOWN_LOCATION", "Pickup or dropoff zone is 264/265 (Unknown / Outside NYC)"),
    (512, "DUPLICATE", "Exact duplicate of an earlier record"),
]
# ---- WARN rules: kept in metrics, reported, and tested via sensitivity checks
WARN_RULES = [
    (1, "PAYMENT_TYPE_0", "payment_type 0 with null passenger_count/RatecodeID (unrecorded or flex-fare trip)"),
    (2, "UNKNOWN_RATECODE", "RatecodeID = 99 (unknown rate)"),
    (4, "ZERO_PASSENGERS", "passenger_count = 0"),
    (8, "HAS_REVERSAL", "Trip has a mirror-image negative record - revenue was reversed; excluded from revenue metrics"),
]
EXCLUDE_BITS = {code: bit for bit, code, _ in EXCLUDE_RULES}
WARN_BITS = {code: bit for bit, code, _ in WARN_RULES}

def has_bit(flags: pd.Series, bit: int) -> pd.Series:
    return (flags & bit) > 0


TRIP_KEY = ["VendorID", "tpep_pickup_datetime", "tpep_dropoff_datetime", "PULocationID", "DOLocationID"]


def load_trips(path, month: str) -> pd.DataFrame:
    import pyarrow.parquet as pq
    available = set(pq.ParquetFile(path).schema_arrow.names)
    missing = [c for c in config.TRIP_COLUMNS if c not in available]
    if missing:
        # Schema drift is a hard failure: we will not guess what a renamed column means.
        raise PipelineError(f"{month}: required columns missing from source: {missing}")
    cols = config.TRIP_COLUMNS + [c for c in config.OPTIONAL_TRIP_COLUMNS if c in available]
    return pd.read_parquet(path, columns=cols)


def apply_rules(df: pd.DataFrame, month: str) -> pd.DataFrame:
    """Add duration/speed and the two flag columns. Does not drop or alter source values."""
    df = df.copy()
    pu, do = df["tpep_pickup_datetime"], df["tpep_dropoff_datetime"]
    df["duration_s"] = (do - pu).dt.total_seconds()
    with np.errstate(divide="ignore", invalid="ignore"):
        df["speed_mph"] = np.where(df["duration_s"] > 0,
                                   df["trip_distance"] / (df["duration_s"] / 3600), np.nan)

    dq = np.zeros(len(df), dtype=np.int32)

    def flag(mask, code):
        nonlocal dq
        dq |= np.where(mask, EXCLUDE_BITS[code], 0).astype(np.int32)

    flag(pu.dt.strftime("%Y-%m") != month, "OUT_OF_PERIOD")
    flag(df["duration_s"] <= 0, "NON_POSITIVE_DURATION")
    flag((df["duration_s"] > 0) & (df["duration_s"] < config.MIN_DURATION_S), "TOO_SHORT")
    flag(df["duration_s"] > config.MAX_DURATION_S, "TOO_LONG")
    flag(~(df["trip_distance"] > 0), "ZERO_DISTANCE")
    flag(df["trip_distance"] > config.MAX_DISTANCE_MI, "IMPLAUSIBLE_DISTANCE")
    flag(df["speed_mph"] > config.MAX_SPEED_MPH, "IMPLAUSIBLE_SPEED")
    flag(df["fare_amount"] < 0, "NEGATIVE_FARE")
    unknown = list(config.UNKNOWN_ZONE_IDS)
    flag(df["PULocationID"].isin(unknown) | df["DOLocationID"].isin(unknown), "UNKNOWN_LOCATION")
    dup_cols = TRIP_KEY + ["trip_distance", "fare_amount", "total_amount"]
    flag(df.duplicated(dup_cols, keep="first").to_numpy(), "DUPLICATE")

    warn = np.zeros(len(df), dtype=np.int32)
    warn |= np.where(df["payment_type"] == 0, WARN_BITS["PAYMENT_TYPE_0"], 0).astype(np.int32)
    warn |= np.where(df["RatecodeID"] == 99, WARN_BITS["UNKNOWN_RATECODE"], 0).astype(np.int32)
    warn |= np.where(df["passenger_count"] == 0, WARN_BITS["ZERO_PASSENGERS"], 0).astype(np.int32)

    # Reversal twins: a positive trip whose key matches a negative record with the mirrored fare.
    neg = df.loc[df["fare_amount"] < 0, TRIP_KEY + ["fare_amount"]].copy()
    neg["fare_amount"] = -neg["fare_amount"]
    neg = neg.drop_duplicates()
    neg["_rev"] = True
    pos_idx = df.index[df["fare_amount"] > 0]
    matched = (df.loc[pos_idx, TRIP_KEY + ["fare_amount"]].reset_index()
               .merge(neg, on=TRIP_KEY + ["fare_amount"], how="inner")["index"])
    has_rev = df.index.isin(matched)
    warn |= np.where(has_rev, WARN_BITS["HAS_REVERSAL"], 0).astype(np.int32)

    df["dq_flags"] = dq
    df["warn_flags"] = warn
    df["is_valid"] = (dq == 0).astype(np.int8)
    return df


# ---------------------------------------------------------------- quality gates
def check_crz_definition(df: pd.DataFrame) -> dict | None:
    """Cross-check the assumed CRZ zone list against the toll flag the source started
    publishing in 2025. Intra-zone trips (PU == DO) either lie wholly inside the zone or not,
    so the toll should be charged iff the zone is in our CRZ list."""
    if "cbd_congestion_fee" not in df.columns:
        return None
    sample = df[(df["is_valid"] == 1)
                & (df["PULocationID"] == df["DOLocationID"])
                & (df["tpep_pickup_datetime"] >= config.CRZ_START_DATE)]
    if sample.empty:
        return None
    in_list = sample["PULocationID"].isin(config.CRZ_ZONE_IDS)
    charged = sample["cbd_congestion_fee"] > 0
    agreement = float((in_list == charged).mean())
    per_zone = (sample.assign(charged=charged, in_list=in_list)
                .groupby("PULocationID").agg(trips=("charged", "size"),
                                             toll_rate=("charged", "mean"),
                                             in_list=("in_list", "first")))
    per_zone = per_zone[per_zone["trips"] >= 200]
    disagree = per_zone[(per_zone["toll_rate"] > 0.5) != per_zone["in_list"]]
    return {
        "intra_zone_trips_checked": int(len(sample)),
        "agreement": round(agreement, 4),
        "zones_disagreeing": {int(z): round(float(r), 3) for z, r in disagree["toll_rate"].items()},
        "min_toll_rate_in_list": round(float(per_zone.loc[per_zone["in_list"], "toll_rate"].min()), 3),
        "max_toll_rate_outside_list": round(float(per_zone.loc[~per_zone["in_list"], "toll_rate"].max()), 3),
    }


def run_gates(df: pd.DataFrame, month: str, expected_rows: int) -> tuple[list[dict], dict | None]:
    gates = []

    def gate(name, passed, observed, threshold):
        gates.append({"gate": name, "passed": bool(passed), "observed": observed, "threshold": threshold})

    gate("row_count_matches_source_footer", len(df) == expected_rows, len(df), expected_rows)

    oop = float(has_bit(df["dq_flags"], EXCLUDE_BITS["OUT_OF_PERIOD"]).mean())
    gate("out_of_period_rate", oop <= config.MAX_OUT_OF_MONTH_RATE, round(oop, 5),
         f"<= {config.MAX_OUT_OF_MONTH_RATE}")

    in_month = df.loc[~has_bit(df["dq_flags"], EXCLUDE_BITS["OUT_OF_PERIOD"]), "tpep_pickup_datetime"]
    daily = in_month.dt.date.value_counts()
    expected_days = pd.Period(month).days_in_month
    gate("every_day_present", len(daily) == expected_days, len(daily), expected_days)
    min_day = int(daily.min()) if len(daily) else 0
    gate("min_trips_per_day", min_day >= config.MIN_TRIPS_PER_DAY, min_day,
         f">= {config.MIN_TRIPS_PER_DAY}")

    valid_rate = float(df["is_valid"].mean())
    gate("valid_trip_rate", valid_rate >= config.MIN_VALID_RATE, round(valid_rate, 4),
         f">= {config.MIN_VALID_RATE}")

    crz = check_crz_definition(df)
    if crz is not None:
        gate("crz_zone_list_matches_toll_flag", crz["agreement"] >= config.CRZ_FEE_AGREEMENT_MIN,
             crz["agreement"], f">= {config.CRZ_FEE_AGREEMENT_MIN}")
    return gates, crz


def build_report(df: pd.DataFrame, month: str, gates: list, crz: dict | None) -> dict:
    n = len(df)
    excl = [{"bit": b, "rule": c, "meaning": m,
             "rows": int(has_bit(df["dq_flags"], b).sum()),
             "pct": round(100 * float(has_bit(df["dq_flags"], b).mean()), 3)}
            for b, c, m in EXCLUDE_RULES]
    warn = [{"bit": b, "rule": c, "meaning": m,
             "rows": int(has_bit(df["warn_flags"], b).sum()),
             "pct": round(100 * float(has_bit(df["warn_flags"], b).mean()), 3)}
            for b, c, m in WARN_RULES]
    return {
        "month": month,
        "rows_total": n,
        "rows_valid": int(df["is_valid"].sum()),
        "valid_rate": round(float(df["is_valid"].mean()), 4),
        "exclude_rules": excl,
        "warn_rules": warn,
        "gates": gates,
        "crz_definition_check": crz,
        "published": all(g["passed"] for g in gates),
    }


def write_report(report: dict) -> None:
    month = report["month"]
    out = config.OUTPUT_DIR / "validation"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"validation_{month}.json").write_text(json.dumps(report, indent=2))
    lines = [f"# Validation report - {month}", "",
             f"Rows: **{report['rows_total']:,}** | valid: **{report['rows_valid']:,}** "
             f"({100 * report['valid_rate']:.2f}%) | published: **{report['published']}**", "",
             "## Quality gates (any failure blocks publication)", "",
             "| Gate | Observed | Threshold | Result |", "|---|---|---|---|"]
    for g in report["gates"]:
        lines.append(f"| {g['gate']} | {g['observed']} | {g['threshold']} | "
                     f"{'PASS' if g['passed'] else '**FAIL**'} |")
    lines += ["", "## Exclude rules (row kept in warehouse, flagged, left out of metrics)", "",
              "| Rule | Meaning | Rows | % |", "|---|---|---|---|"]
    for r in report["exclude_rules"]:
        lines.append(f"| {r['rule']} | {r['meaning']} | {r['rows']:,} | {r['pct']} |")
    lines += ["", "Rules overlap, so a row can break several; the percentages do not sum to the exclusion rate.", "",
              "## Warn rules (row kept in metrics; caveat recorded)", "",
              "| Rule | Meaning | Rows | % |", "|---|---|---|---|"]
    for r in report["warn_rules"]:
        lines.append(f"| {r['rule']} | {r['meaning']} | {r['rows']:,} | {r['pct']} |")
    if report["crz_definition_check"]:
        c = report["crz_definition_check"]
        lines += ["", "## CRZ zone-list cross-check against `cbd_congestion_fee`", "",
                  f"- Intra-zone trips checked: {c['intra_zone_trips_checked']:,}",
                  f"- Agreement between our zone list and the toll flag: **{100 * c['agreement']:.2f}%**",
                  f"- Lowest toll rate for a zone in our list: {c['min_toll_rate_in_list']}",
                  f"- Highest toll rate for a zone outside our list: {c['max_toll_rate_outside_list']}",
                  f"- Zones where list and toll flag disagree (>=200 trips): {c['zones_disagreeing'] or 'none'}"]
    (out / f"validation_{month}.md").write_text("\n".join(lines) + "\n")


def write_quarantine_sample(df: pd.DataFrame, month: str, per_rule: int = 20) -> None:
    """Evidence for reviewers: a few example rows for each exclude rule."""
    parts = []
    for bit, code, _ in EXCLUDE_RULES:
        s = df[has_bit(df["dq_flags"], bit)].head(per_rule).copy()
        s.insert(0, "rule", code)
        parts.append(s)
    out = config.OUTPUT_DIR / "validation"
    out.mkdir(parents=True, exist_ok=True)
    pd.concat(parts).to_csv(out / f"quarantine_sample_{month}.csv", index=False)


def profile(df: pd.DataFrame, month: str) -> None:
    """Column-level profile of the raw trip file, written before any rules are applied."""
    rows = []
    for col in df.columns:
        s = df[col]
        row = {"column": col, "dtype": str(s.dtype), "nulls": int(s.isna().sum()),
               "null_pct": round(100 * float(s.isna().mean()), 2), "distinct": int(s.nunique())}
        if pd.api.types.is_numeric_dtype(s) or pd.api.types.is_datetime64_any_dtype(s):
            row["min"], row["max"] = s.min(), s.max()
        if pd.api.types.is_numeric_dtype(s):
            row["p50"], row["p99"] = s.quantile(0.5), s.quantile(0.99)
        rows.append(row)
    prof = pd.DataFrame(rows)
    out = config.OUTPUT_DIR / "profile"
    out.mkdir(parents=True, exist_ok=True)
    prof.to_csv(out / f"profile_{month}.csv", index=False)
    lines = [f"# Raw profile - yellow_tripdata_{month}", "", f"Rows: {len(df):,}", "",
             "| " + " | ".join(prof.columns) + " |", "|" + "---|" * len(prof.columns)]
    for _, r in prof.iterrows():
        lines.append("| " + " | ".join("" if pd.isna(v) else str(v) for v in r.values) + " |")
    (out / f"profile_{month}.md").write_text("\n".join(lines) + "\n")
