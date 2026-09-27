"""Fast tests on synthetic data - no network, no real files.   Run:  python -m pytest -q"""
import json

import pandas as pd
import pytest

from pipeline import config, metrics, model, validate
from pipeline.validate import EXCLUDE_BITS, WARN_BITS, has_bit

MONTH = "2025-01"


def trip(pu="2025-01-10 08:00:00", dur_s=600, dist=2.0, fare=12.0, pu_zone=161, do_zone=230,
         payment=1, rate=1.0, pax=1, vendor=2):
    p = pd.Timestamp(pu)
    return {"VendorID": vendor, "tpep_pickup_datetime": p,
            "tpep_dropoff_datetime": p + pd.Timedelta(seconds=dur_s), "passenger_count": pax,
            "trip_distance": dist, "RatecodeID": rate, "PULocationID": pu_zone, "DOLocationID": do_zone,
            "payment_type": payment, "fare_amount": fare, "tip_amount": 2.0, "total_amount": fare + 5,
            "cbd_congestion_fee": 0.75}


def flags_of(rows):
    return validate.apply_rules(pd.DataFrame(rows), MONTH)


@pytest.mark.parametrize("kwargs,rule", [
    ({"pu": "2002-12-31 22:00:00"}, "OUT_OF_PERIOD"),
    ({"dur_s": -30}, "NON_POSITIVE_DURATION"),
    ({"dur_s": 30}, "TOO_SHORT"),
    ({"dur_s": 4 * 3600}, "TOO_LONG"),
    ({"dist": 0}, "ZERO_DISTANCE"),
    ({"dist": 5000, "dur_s": 3 * 3600 - 1}, "IMPLAUSIBLE_DISTANCE"),
    ({"dist": 30, "dur_s": 600}, "IMPLAUSIBLE_SPEED"),
    ({"fare": -12.0}, "NEGATIVE_FARE"),
    ({"pu_zone": 264}, "UNKNOWN_LOCATION"),
])
def test_each_exclude_rule_fires(kwargs, rule):
    df = flags_of([trip(**kwargs)])
    assert has_bit(df["dq_flags"], EXCLUDE_BITS[rule]).iloc[0]
    assert df["is_valid"].iloc[0] == 0


def test_clean_trip_is_valid_and_rows_are_never_dropped():
    df = flags_of([trip(), trip(dur_s=-5), trip(fare=-1)])
    assert len(df) == 3                      # nothing silently removed
    assert df["is_valid"].tolist() == [1, 0, 0]


def test_duplicate_flags_only_the_second_copy():
    df = flags_of([trip(), trip()])
    assert df["dq_flags"].tolist()[0] == 0
    assert has_bit(df["dq_flags"], EXCLUDE_BITS["DUPLICATE"]).iloc[1]


def test_reversal_twin_is_warned_not_excluded():
    df = flags_of([trip(fare=15.0), trip(fare=-15.0)])
    assert df["is_valid"].tolist() == [1, 0]
    assert has_bit(df["warn_flags"], WARN_BITS["HAS_REVERSAL"]).iloc[0]


def test_payment_type_0_is_a_warning_only():
    df = flags_of([trip(payment=0, pax=None, rate=None)])
    assert df["is_valid"].iloc[0] == 1
    assert has_bit(df["warn_flags"], WARN_BITS["PAYMENT_TYPE_0"]).iloc[0]


def test_segments():
    zones = pd.DataFrame({"zone_id": [161, 230, 236, 138],
                          "borough": ["Manhattan", "Manhattan", "Manhattan", "Queens"],
                          "is_crz": [1, 1, 0, 0]})
    seg = model.assign_segment(pd.Series([161, 138, 161, 236, 138]), pd.Series([230, 161, 138, 236, 138]), zones)
    assert list(seg) == ["crz_internal", "crz_inbound", "crz_outbound", "manhattan_non_crz", "other"]


def test_gate_fails_on_out_of_period_file():
    rows = [trip(pu="2024-06-10 08:00:00") for _ in range(10)]
    df = flags_of(rows)
    gates, _ = validate.run_gates(df, MONTH, expected_rows=10)
    failed = {g["gate"] for g in gates if not g["passed"]}
    assert {"out_of_period_rate", "valid_trip_rate"} <= failed


def test_gate_fails_on_row_count_mismatch():
    df = flags_of([trip()])
    gates, _ = validate.run_gates(df, MONTH, expected_rows=999)
    assert not next(g for g in gates if g["gate"] == "row_count_matches_source_footer")["passed"]


@pytest.fixture
def raw_files(tmp_path):
    zones = tmp_path / "zones.csv"
    pd.DataFrame({"LocationID": [161, 230, 236], "Borough": ["Manhattan"] * 3,
                  "Zone": ["Midtown Center", "Times Sq", "UES North"],
                  "service_zone": ["Yellow Zone"] * 3}).to_csv(zones, index=False)
    days = pd.date_range("2025-01-01", "2025-01-31").strftime("%Y-%m-%d").tolist()
    weather = tmp_path / "w.json"
    weather.write_text(json.dumps({"daily": {"time": days, "precipitation_sum": [0.0] * 31,
                                             "snowfall_sum": [0.0] * 31, "temperature_2m_mean": [1.0] * 31}}))
    hol = tmp_path / "h.json"
    hol.write_text(json.dumps([{"date": "2025-01-01", "name": "New Year's Day", "global": True}]))
    return {"zones": zones, "weather": weather, "holidays": hol}


def test_model_load_is_idempotent_and_metrics_compute(tmp_path, monkeypatch, raw_files):
    monkeypatch.setattr(config, "WAREHOUSE_PATH", tmp_path / "wh.sqlite")
    conn = model.connect()
    speeds = [(600, 1.0), (600, 2.0), (600, 3.0)]  # 6, 12, 18 mph
    df = flags_of([trip(dur_s=d, dist=x, pu=f"2025-01-1{3 + i} 09:00:00")  # Mon-Wed
                   for i, (d, x) in enumerate(speeds)])
    model.load_month(conn, MONTH, df, raw_files)
    model.load_month(conn, MONTH, df, raw_files)  # rerun must not duplicate
    assert conn.execute("SELECT COUNT(*) FROM fact_trip").fetchone()[0] == 3
    res = metrics.compute(conn, MONTH, "2025-01-01", "2025-01-31")
    assert res["crz_median_speed_mph"] == (12.0, 3)
    assert res["valid_trip_rate"][0] == 1.0
    assert res["crz_fare_per_engaged_hour"][0] == pytest.approx(36 / 0.5)
    conn.close()
