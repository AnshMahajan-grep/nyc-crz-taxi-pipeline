"""Pipeline entry point:  ingest -> profile/validate -> model -> metrics -> evidence.

    python -m pipeline.run                       # default months (2024-01, 2025-01)
    python -m pipeline.run --months 2025-02      # any month TLC has published
    python -m pipeline.run --force-download      # ignore cached raw files

Dependability:
  * Every stage is logged to logs/pipeline_<run_id>.log and outputs/run_history.csv.
  * A month that fails a quality gate is NOT loaded or published; other months continue.
  * Evidence is regenerated only when every requested month succeeded, so a partial run
    never overwrites good evidence with half-updated numbers.
  * Exit code 0 = all good, 1 = at least one month failed (usable by a scheduler/CI).
"""
import argparse
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone

from . import config, ingest, metrics, model, report, validate
from .errors import PipelineError

log = logging.getLogger("pipeline")


def setup_logging(run_id: str) -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.FileHandler(config.LOG_DIR / f"pipeline_{run_id}.log", encoding="utf-8"),
              logging.StreamHandler(sys.stdout)):
        h.setFormatter(fmt)
        root.addHandler(h)


class StageLog:
    """Appends one row per stage to outputs/run_history.csv."""
    path = config.OUTPUT_DIR / "run_history.csv"
    fields = ["run_id", "month", "stage", "status", "seconds", "rows", "detail"]

    def __init__(self, run_id):
        self.run_id = run_id
        config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with open(self.path, "w", newline="") as f:
                csv.writer(f).writerow(self.fields)

    def write(self, month, stage, status, seconds, rows="", detail=""):
        with open(self.path, "a", newline="") as f:
            csv.writer(f).writerow([self.run_id, month, stage, status, round(seconds, 1), rows, detail])


def run_month(month: str, force: bool, conn, stages: StageLog) -> dict:
    def stage(name, fn):
        t0 = time.time()
        log.info("[%s] %s: start", month, name)
        try:
            result, rows = fn()
        except Exception as exc:
            stages.write(month, name, "FAILED", time.time() - t0, detail=str(exc)[:300])
            raise
        stages.write(month, name, "OK", time.time() - t0, rows)
        log.info("[%s] %s: done in %.1fs", month, name, time.time() - t0)
        return result

    raw = stage("ingest", lambda: (ingest.ingest_month(month, force), ""))
    expected_rows = ingest.manifest_entry(f"yellow_tripdata_{month}")["parquet_rows"]

    trips = stage("load_raw", lambda: (lambda d: (d, len(d)))(validate.load_trips(raw["trips"], month)))
    stage("profile", lambda: (validate.profile(trips, month), len(trips)))
    trips = stage("validate", lambda: (lambda d: (d, int(d["is_valid"].sum())))(validate.apply_rules(trips, month)))

    def gates():
        g, crz = validate.run_gates(trips, month, expected_rows)
        rep = validate.build_report(trips, month, g, crz)
        validate.write_report(rep)
        validate.write_quarantine_sample(trips, month)
        failed = [x["gate"] for x in g if not x["passed"]]
        for x in g:
            log.log(logging.INFO if x["passed"] else logging.ERROR, "[%s] gate %-34s %s (observed %s, threshold %s)",
                    month, x["gate"], "PASS" if x["passed"] else "FAIL", x["observed"], x["threshold"])
        if failed:
            raise PipelineError(f"{month}: quality gates failed {failed} - month not published")
        return rep, len(g)
    rep = stage("quality_gates", gates)

    stage("model", lambda: (None, model.load_month(conn, month, trips, raw)))
    res = stage("metrics", lambda: (metrics.publish_monthly(conn, month), len(metrics.CATALOGUE)))
    return {"valid_rate": rep["valid_rate"], "metrics": {k: v for k, (v, _) in res.items()}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--months", nargs="+", default=config.DEFAULT_MONTHS, help="YYYY-MM, oldest first")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--window-start-day", type=int, default=5,
                    help="evidence compares day N..end of month in each month (default 5 = toll start day)")
    args = ap.parse_args(argv)

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    setup_logging(run_id)
    stages = StageLog(run_id)
    log.info("Run %s starting for months %s", run_id, args.months)
    for d in (config.RAW_DIR, config.PROCESSED_DIR, config.OUTPUT_DIR):
        d.mkdir(parents=True, exist_ok=True)

    conn = model.connect()
    results, failures = {}, {}
    for month in args.months:
        try:
            results[month] = run_month(month, args.force_download, conn, stages)
        except PipelineError as exc:
            log.error("[%s] FAILED: %s", month, exc)
            failures[month] = str(exc)
        except Exception as exc:  # unexpected: keep traceback in the log, carry on with other months
            log.exception("[%s] FAILED with unexpected error", month)
            failures[month] = f"{type(exc).__name__}: {exc}"

    if failures:
        log.error("Evidence NOT regenerated because %d month(s) failed: %s", len(failures), list(failures))
    elif len(args.months) >= 2:
        t0 = time.time()
        report.build_evidence(conn, args.months[0], args.months[-1], args.window_start_day, run_id)
        stages.write("all", "evidence", "OK", time.time() - t0)
    conn.close()

    summary = {"run_id": run_id, "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "months": args.months, "succeeded": list(results), "failed": failures, "results": results}
    (config.OUTPUT_DIR / "run_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    log.info("Run %s finished: %d ok, %d failed", run_id, len(results), len(failures))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
