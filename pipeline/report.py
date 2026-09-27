"""Stage 5 - Evidence output: comparison table (MD + CSV) and one chart.

Baseline and treatment are compared over the same day-of-month window (default day 5 to
month end) so that January 2025's pre-toll days (1-4 Jan) are not mixed into "after".
"""
import logging
from datetime import datetime, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from . import config, metrics

log = logging.getLogger(__name__)

# Reference palette (dataviz skill, validated: all checks pass on #fcfcfb)
BASELINE_COLOR, TREATMENT_COLOR = "#2a78d6", "#eb6834"
INK, INK_2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"

FORMATS = {
    "valid_trip_rate": "{:.2%}",
    "crz_median_speed_mph": "{:.2f} mph",
    "control_median_speed_mph": "{:.2f} mph",
    "crz_p90_duration_min": "{:.1f} min",
    "crz_fare_per_engaged_hour": "${:.2f}/h",
    "crz_touching_trips_per_weekday": "{:,.0f}",
    "crz_median_speed_dry_days": "{:.2f} mph",
    "control_median_speed_dry_days": "{:.2f} mph",
    "crz_median_speed_excl_pt0": "{:.2f} mph",
}


def _window(month: str, start_day: int) -> tuple[str, str]:
    _, end = metrics.month_bounds(month)
    return f"{month}-{start_day:02d}", end


def _context(conn, month, d_from, d_to) -> dict:
    row = conn.execute("""
        SELECT SUM(is_weekend = 0 AND is_holiday = 0), SUM(is_wet_day), SUM(snowfall_cm > 0),
               ROUND(AVG(temp_mean_c), 1), SUM(precip_mm)
        FROM dim_date WHERE year_month = ? AND date BETWEEN ? AND ?""", (month, d_from, d_to)).fetchone()
    return {"weekdays": row[0], "wet_days": row[1], "snow_days": row[2], "avg_temp_c": row[3],
            "precip_mm": round(row[4] or 0, 1)}


def _chart(conn, base, treat, bw, tw) -> None:
    b = metrics.hourly_speed_profile(conn, base, *bw)
    t = metrics.hourly_speed_profile(conn, treat, *tw)
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    lo, hi = min(config.PEAK_WEEKDAY_HOURS), max(config.PEAK_WEEKDAY_HOURS) + 1
    ax.axvspan(lo - 0.5, hi - 0.5, color="#f1f0ec", zorder=0, lw=0)
    ax.text(lo - 0.3, 0.02, "toll peak window (KPI)", transform=ax.get_xaxis_transform(),
            color=MUTED, fontsize=8)
    # Direct labels go where the two lines are furthest apart, so they never collide.
    gap_hour = int((b - t).abs().idxmax())
    upper = b if b[gap_hour] >= t[gap_hour] else t
    for s, color, label in ((b, BASELINE_COLOR, f"{base} (before toll)"),
                            (t, TREATMENT_COLOR, f"{treat} (toll live)")):
        ax.plot(s.index, s.values, color=color, lw=2, marker="o", ms=4.5,
                markeredgecolor=SURFACE, markeredgewidth=1.2, label=label, zorder=3)
        above = s is upper
        ax.annotate(label, (gap_hour, s[gap_hour]), xytext=(0, 10 if above else -14),
                    textcoords="offset points", ha="center", va="center", fontsize=8.5, color=INK_2)
    ax.set_xticks(range(0, 24, 2))
    ax.set_xticklabels([f"{h:02d}:00" for h in range(0, 24, 2)], fontsize=8, color=MUTED)
    ax.tick_params(axis="y", labelsize=8, colors=MUTED, length=0)
    ax.tick_params(axis="x", length=0)
    ax.set_ylabel("Median speed (mph)", fontsize=9, color=INK_2)
    ax.set_xlabel("Pickup hour (weekdays, non-holiday)", fontsize=9, color=INK_2)
    ax.grid(axis="y", color=GRID, lw=0.8)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(AXIS)
    ax.set_xlim(-0.5, 23.5)
    ax.legend(frameon=False, fontsize=8.5, loc="upper right", ncol=1, labelcolor=INK_2)
    ax.set_title("Trips inside the Congestion Relief Zone: median speed by hour",
                 loc="left", fontsize=11, color=INK, pad=14)
    fig.tight_layout()
    fig.savefig(config.OUTPUT_DIR / "crz_speed_by_hour.png", facecolor=SURFACE)
    plt.close(fig)
    pd.DataFrame({base: b, treat: t}).round(3).to_csv(config.OUTPUT_DIR / "crz_speed_by_hour.csv")


def build_evidence(conn, base: str, treat: str, start_day: int, run_id: str) -> pd.DataFrame:
    bw, tw = _window(base, start_day), _window(treat, start_day)
    rb, rt = metrics.compute(conn, base, *bw), metrics.compute(conn, treat, *tw)
    rows = []
    for name, (definition, question) in metrics.CATALOGUE.items():
        (vb, nb), (vt, nt) = rb[name], rt[name]
        pct = (vt - vb) / vb if vb else None
        rows.append({"metric": name, "definition": definition, "question": question,
                     "baseline": vb, "treatment": vt, "abs_change": vt - vb, "pct_change": pct,
                     "n_baseline": nb, "n_treatment": nt})
    ev = pd.DataFrame(rows)
    ev.to_csv(config.OUTPUT_DIR / "evidence_table.csv", index=False)

    kpi = ev.set_index("metric")
    net = kpi.at["crz_median_speed_mph", "pct_change"] - kpi.at["control_median_speed_mph", "pct_change"]
    net_dry = (kpi.at["crz_median_speed_dry_days", "pct_change"]
               - kpi.at["control_median_speed_dry_days", "pct_change"])
    cb, ct = _context(conn, base, *bw), _context(conn, treat, *tw)

    fmt = lambda m, v: "n/a" if v is None or pd.isna(v) else FORMATS[m].format(v)
    lines = [
        "# Evidence table - CRZ congestion pricing vs taxi trip performance", "",
        f"_Generated {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC} by pipeline run `{run_id}` "
        f"- do not edit by hand; rerun `python -m pipeline.run`._", "",
        f"**Baseline:** {bw[0]} to {bw[1]} (no toll) | **Treatment:** {tw[0]} to {tw[1]} (toll live from {config.CRZ_START_DATE})", "",
        "| # | Metric | Baseline | Treatment | Change | Trips (base / treat) | Business question |",
        "|---|---|---|---|---|---|---|",
    ]
    for i, r in enumerate(ev.itertuples(), 1):
        pct = "n/a" if r.pct_change is None or pd.isna(r.pct_change) else f"{r.pct_change:+.1%}"
        if r.metric == "valid_trip_rate":
            pct = f"{100 * r.abs_change:+.2f} pp"
        lines.append(f"| {i} | **{r.metric}** | {fmt(r.metric, r.baseline)} | {fmt(r.metric, r.treatment)} | "
                     f"{pct} | {r.n_baseline:,} / {r.n_treatment:,} | {r.question} |")
    lines += [
        "", f"**Net KPI effect vs control (difference in % change): {net:+.1%}** "
        "- the part of the CRZ speed change not explained by the change seen outside the zone.", "",
        f"**Net KPI effect vs control, dry days only: {net_dry:+.1%}** "
        "- the same comparison with rain/snow days removed from both groups and both years.", "",
        "## Metric definitions", "",
    ]
    lines += [f"- **{m}** - {d}" for m, (d, _) in metrics.CATALOGUE.items()]
    lines += [
        "", "Population for metrics 2-5 and 7-9 (metric 5 also excludes reversed trips): valid trips, non-holiday weekdays, pickup "
        f"{min(config.PEAK_WEEKDAY_HOURS):02d}:00-{max(config.PEAK_WEEKDAY_HOURS):02d}:59. Metric 1 covers every record in the month.", "",
        "## Context (from the weather + holiday APIs)", "",
        "| Window | Working weekdays | Wet days (>=1 mm) | Snow days | Total precip (mm) | Mean temp (C) |",
        "|---|---|---|---|---|---|",
        f"| {base} | {cb['weekdays']} | {cb['wet_days']} | {cb['snow_days']} | {cb['precip_mm']} | {cb['avg_temp_c']} |",
        f"| {treat} | {ct['weekdays']} | {ct['wet_days']} | {ct['snow_days']} | {ct['precip_mm']} | {ct['avg_temp_c']} |",
        "", "![CRZ speed by hour](crz_speed_by_hour.png)", "",
        "Hourly values: `crz_speed_by_hour.csv`. Validation detail: `validation/validation_<month>.md`.",
    ]
    (config.OUTPUT_DIR / "evidence_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    _chart(conn, base, treat, bw, tw)
    log.info("Evidence written: KPI %s -> %s (%+.1f%%), net of control %+.1f%%",
             fmt("crz_median_speed_mph", kpi.at["crz_median_speed_mph", "baseline"]),
             fmt("crz_median_speed_mph", kpi.at["crz_median_speed_mph", "treatment"]),
             100 * kpi.at["crz_median_speed_mph", "pct_change"], 100 * net)
    return ev
