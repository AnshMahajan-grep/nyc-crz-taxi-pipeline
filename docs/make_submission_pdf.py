"""Build the 2-page submission PDF from the pipeline's outputs.

    python docs/make_submission_pdf.py --github https://github.com/AnshMahajan-grep/nyc-crz-taxi-pipeline
"""
import argparse
import csv
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (Image, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table,
                                TableStyle)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "submission" / "10345_Ansh_Mahajan.pdf"
NAVY, BLUE, LIGHT, GRID = colors.HexColor("#183a5a"), colors.HexColor("#2a78d6"), \
    colors.HexColor("#eef3f9"), colors.HexColor("#c3c2b7")

H = ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=10.5, textColor=NAVY, spaceBefore=5, spaceAfter=2.5)
B = ParagraphStyle("b", fontName="Helvetica", fontSize=8, leading=10.2)
S = ParagraphStyle("s", parent=B, fontSize=7.3, leading=9)
SB = ParagraphStyle("sb", parent=S, fontName="Helvetica-Bold", textColor=colors.white)
BOX = ParagraphStyle("box", parent=S, alignment=TA_CENTER)


def table(rows, widths, header=True, font=S, zebra=True):
    data = [[Paragraph(str(c), SB if header and i == 0 else font) for c in r] for i, r in enumerate(rows)]
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    style = [("GRID", (0, 0), (-1, -1), 0.4, GRID), ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("TOPPADDING", (0, 0), (-1, -1), 1.8), ("BOTTOMPADDING", (0, 0), (-1, -1), 1.8),
             ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]
    if header:
        style.append(("BACKGROUND", (0, 0), (-1, 0), NAVY))
    if zebra:
        style += [("BACKGROUND", (0, i), (-1, i), LIGHT) for i in range(2 if header else 1, len(rows), 2)]
    t.setStyle(TableStyle(style))
    return t


def flow(steps, width):
    """A left-to-right box diagram: step -> step -> ..."""
    cells, widths = [], []
    box_w = (width - 0.45 * cm * (len(steps) - 1)) / len(steps)
    for i, s in enumerate(steps):
        cells.append(Paragraph(s, BOX)); widths.append(box_w)
        if i < len(steps) - 1:
            cells.append(Paragraph("<b>&rarr;</b>", BOX)); widths.append(0.45 * cm)
    t = Table([cells], colWidths=widths)
    st = [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 2),
          ("RIGHTPADDING", (0, 0), (-1, -1), 2)]
    for i in range(0, len(cells), 2):
        st += [("BOX", (i, 0), (i, 0), 0.8, BLUE), ("BACKGROUND", (i, 0), (i, 0), LIGHT)]
    t.setStyle(TableStyle(st))
    return t


def evidence_rows():
    labels = {
        "valid_trip_rate": "Valid trip rate (trust)",
        "crz_median_speed_mph": "<b>KPI: CRZ median speed, weekday peak</b>",
        "control_median_speed_mph": "Control: Manhattan outside CRZ",
        "crz_p90_duration_min": "CRZ p90 trip duration",
        "crz_fare_per_engaged_hour": "CRZ fare per engaged hour",
        "crz_touching_trips_per_weekday": "CRZ-touching trips / weekday",
        "crz_median_speed_dry_days": "KPI, dry days only",
        "control_median_speed_dry_days": "Control, dry days only",
        "crz_median_speed_excl_pt0": "KPI excl. payment_type 0",
    }
    fmt = {"valid_trip_rate": "{:.2%}", "crz_p90_duration_min": "{:.1f} min",
           "crz_fare_per_engaged_hour": "${:.2f}", "crz_touching_trips_per_weekday": "{:,.0f}"}
    rows = [["Metric", "Jan 2024", "Jan 2025", "Change"]]
    with open(ROOT / "outputs" / "evidence_table.csv") as f:
        for r in csv.DictReader(f):
            m = r["metric"]
            f_ = fmt.get(m, "{:.2f} mph")
            b, t = float(r["baseline"]), float(r["treatment"])
            ch = f"{100 * (t - b):+.2f} pp" if m == "valid_trip_rate" else f"{float(r['pct_change']):+.1%}"
            rows.append([labels[m], f_.format(b), f_.format(t), ch])
    return rows


def build(github: str, loom: str) -> None:
    OUT.parent.mkdir(exist_ok=True)
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=1.3 * cm, rightMargin=1.3 * cm,
                            topMargin=1.1 * cm, bottomMargin=1.0 * cm,
                            title="10345 Ansh Mahajan - FDE Assessment 2", author="Ansh Mahajan")
    W = A4[0] - 2.6 * cm
    st = []
    st.append(Paragraph("Did congestion pricing make NYC taxi trips faster? "
                        "A dependable data pipeline from TLC records to a fleet decision",
                        ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=13.5, leading=16, textColor=NAVY)))
    st.append(Paragraph(f"Ansh Mahajan · Roll 10345 · FDE Data Foundations, Assessment 2 (Track B - NYC TLC) · "
                        f"GitHub: <link href='{github}' color='blue'>{github}</link>", S))

    st.append(Paragraph("1. The problem worth solving (before any tooling)", H))
    st.append(Paragraph(
        "On <b>5 Jan 2025</b> NYC began tolling vehicles entering Manhattan south of 60th St (the "
        "<b>Congestion Relief Zone, CRZ</b>). A yellow-taxi fleet operator must decide whether to <b>reposition drivers "
        "and re-plan shifts</b> around the zone. Leadership's claim is that <i>\"the zone is faster now, so trips pay "
        "better\"</i>. That claim is untested, and acting on it wrongly misallocates the ~70% of weekday "
        "trips that touch the zone. The raw data can't answer it directly. It is split across 4 technology vendors, 4-15% of records are "
        "reversals or meter errors, and the trip files have <b>no CRZ flag before 2025</b>. So the real problem is "
        "a <b>trustworthy, repeatable before/after measurement</b>, not a model or a dashboard.", B))
    st.append(table([
        ["Stakeholder", "Decision / need"],
        ["Fleet operations lead (client)", "Reposition drivers toward CRZ trips in toll hours?"],
        ["Finance / driver relations", "Did revenue per engaged hour change?"],
        ["TLC data owner", "Which records we rejected and why (quality feedback)"],
    ], [5.2 * cm, W - 5.2 * cm]))
    st.append(Spacer(0, 3))
    st.append(Paragraph(
        "<b>Project KPI:</b> median in-trip speed (mph) of trips that start <i>and</i> end inside the CRZ, on "
        "non-holiday weekdays, 05:00-20:59 (toll peak). Measured 5-31 Jan 2024 vs 5-31 Jan 2025, "
        "against a <b>control</b> group (Manhattan trips wholly north of the CRZ, not tolled).", B))

    st.append(Paragraph("2. Sources, retrieval and completeness", H))
    st.append(table([
        ["Source (owner)", "Mode", "Grain", "How completeness is proven"],
        ["Yellow Taxi trip records, monthly Parquet (NYC TLC; filed by 4 vendors)", "File / HTTPS",
         "trip record", "bytes = Content-Length; rows loaded = Parquet footer (2,964,624 / 3,475,226); "
                        "all 31 days present; &gt;= 10k trips/day"],
        ["Taxi zone lookup CSV (TLC)", "File / HTTPS", "zone (265)", "&gt;= 263 zones"],
        ["Open-Meteo historical weather", "REST / JSON", "day", "exactly one record per day"],
        ["Nager.Date public holidays", "REST / JSON", "holiday", "non-empty list"],
        ["Modelled warehouse (this project)", "SQL (SQLite)", "trip/zone/day",
         "rows after insert = validated rows; every metric is a SQL query"],
    ], [5.6 * cm, 2.0 * cm, 1.9 * cm, W - 9.5 * cm]))
    st.append(Paragraph("Raw inputs are stored unaltered with SHA-256 + row counts in <i>data/raw/manifest.json</i>. "
                        "A rerun reuses a raw file only if its checksum still matches.", S))

    st.append(Paragraph("3. Profiling findings and validation rules (flag, never silently fix)", H))
    st.append(table([
        ["Finding (FACT, from profiling)", "Jan 2024", "Jan 2025", "Rule / handling"],
        ["Negative-fare rows: accounting reversals, not trips (83% have an exact mirror positive twin in 2024)",
         "1.26%", "4.15%", "EXCLUDE; the twin is kept for speed but left out of revenue"],
        ["payment_type 0 with null passenger/rate fields; durations and distances look normal", "4.7%", "15.5%",
         "WARN, kept; sensitivity metric without them"],
        ["Zero distance / &lt; 60 s / &gt; 3 h / &gt; 80 mph / &gt; 100 mi", "2.5%", "3.1%",
         "EXCLUDE (meter/odometer/clock errors)"],
        ["Unknown zone 264/265; out-of-month timestamps (e.g. year 2002)", "1.1%", "0.8%", "EXCLUDE"],
        ["<b>Rows passing all rules</b>", "<b>95.5%</b>", "<b>92.6%</b>", "Quality gate: &gt;= 85%, or month not published"],
    ], [8.2 * cm, 1.5 * cm, 1.5 * cm, W - 11.2 * cm]))
    st.append(Paragraph("Rows are never deleted. Every rule sets a bit flag in <i>fact_trip</i>, and counts per rule "
                        "and example rows are published in <i>outputs/validation/</i>.", S))

    st.append(Paragraph("4. Workflow, data model and pipeline", H))
    st.append(flow(["Street hail<br/><i>(not recorded)</i>", "Pickup<br/>time, zone", "In-trip<br/>distance, duration",
                    "Dropoff<br/>time, zone", "Payment<br/>fare, tip, toll", "Reversal<br/><i>(negative row)</i>"], W))
    st.append(Spacer(0, 2))
    st.append(Paragraph("<b>Intervention:</b> CRZ toll (dim_date.is_toll_live, dim_zone.is_crz). "
                        "<b>Confounders:</b> weather, holidays, weekends (dim_date). "
                        "<b>Outcomes:</b> speed, p90 duration, fare/engaged hour, trip volume. "
                        "<b>Star schema:</b> fact_trip (1 row per source record incl. invalid, dq/warn bitmasks, "
                        "segment) - dim_zone - dim_date - metrics_monthly.", S))
    st.append(Spacer(0, 3))
    st.append(flow(["<b>Ingest</b><br/>files + APIs<br/>retry, checksum", "<b>Profile</b><br/>nulls, ranges",
                    "<b>Validate</b><br/>10 exclude, 4 warn<br/>6 quality gates",
                    "<b>Model</b><br/>SQLite partition<br/>1 transaction", "<b>Metrics</b><br/>SQL",
                    "<b>Evidence</b><br/>table + chart"], W))
    st.append(Paragraph("<b>Dependability:</b> stage logs + run_history.csv. A gate failure means that month is not "
                        "published, and the evidence is regenerated only if <i>all</i> months pass. Exit code 1 on failure "
                        "(tested with an unpublished month). Idempotent rerun: month partitions are replaced atomically. "
                        "17 unit tests.", S))

    st.append(PageBreak())
    st.append(Paragraph("5. Evidence: metrics linked to the KPI (5-31 Jan, weekday toll peak)", H))
    ev = table(evidence_rows(), [5.3 * cm, 1.9 * cm, 1.9 * cm, 1.6 * cm])
    img = Image(str(ROOT / "outputs" / "crz_speed_by_hour.png"), width=W - 10.9 * cm,
                height=(W - 10.9 * cm) * 690 / 1350)
    side = Table([[ev, img]], colWidths=[10.9 * cm, W - 10.9 * cm])
    side.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    st.append(side)
    st.append(Spacer(0, 3))
    st.append(Paragraph(
        "<b>Net KPI effect vs control: +3.7% (all days), +3.4% (dry days only).</b> "
        "Raw CRZ speed rose only 1.4%, but the untolled control slowed 2.3%. January 2024 had 12 wet days "
        "(156 mm) against 5 (23 mm) in 2025. On dry days the CRZ is flat while the control slowed 3.3%, "
        "so the <i>relative</i> effect survives the weather check and the raw gain does not. The result also "
        "holds without payment_type 0 records.", B))

    st.append(Paragraph("6. Conclusion and the decision it supports", H))
    st.append(table([
        ["Question", "Answer (evidence)"],
        ["Are zone trips faster?", "Barely in absolute terms (+1.4%, &lt; 1 min per trip). The toll's effect is an "
                                   "<b>avoided slowdown</b> of about 3-4% relative to the rest of Manhattan"],
        ["Did the toll reduce taxi demand in the zone?", "No: +14.5% trips/weekday, in line with overall valid-trip growth (+13.6%)"],
        ["Did drivers earn more per hour?", "Essentially flat: $63.07 to $63.86 per engaged hour (+1.3%)"],
        ["<b>Decision</b>", "<b>Keep or grow driver supply in the CRZ during toll hours</b> (demand and revenue held). "
                            "<b>Don't</b> plan shifts on the assumption that zone trips are faster. Rerun monthly "
                            "(<i>python -m pipeline.run --months ...</i>) before changing the plan."],
    ], [5.0 * cm, W - 5.0 * cm]))

    st.append(Paragraph("7. Facts, assumptions, unknowns and limitations", H))
    st.append(table([
        ["Type", "Statement"],
        ["FACT", "Both trip files are complete (bytes, footer row counts, 31/31 days); all 6 quality gates passed"],
        ["FACT", "Our 38-zone CRZ list agrees with the 2025 toll flag on 93.2% of intra-zone trips; "
                 "every listed zone is tolled on &gt;= 94%, and no unlisted zone above 42%"],
        ["FACT", "Jan 2025 was far drier than Jan 2024 (23 mm vs 156 mm), so weather is a real confounder"],
        ["ASSUMPTION", "The CRZ is approximated by whole taxi zones. Zones straddling 60th St "
                       "(Lincoln Sq, Lenox Hill, Central Park) are counted as outside"],
        ["ASSUMPTION", "Upper Manhattan is a fair control. If it also gained from less through-traffic, our net effect is a lower bound"],
        ["ASSUMPTION", "Jan 2024 is a valid counterfactual after matching weekday, holiday, peak hour and (sensitivity) weather"],
        ["UNKNOWN", "Customer wait/dispatch time (street hails aren't recorded); whether trips passed through the CRZ (no route)"],
        ["UNKNOWN", "Why payment_type 0 records tripled - to be raised with TLC/vendors"],
        ["LIMITATION", "One month before and after; yellow taxis only (not Uber/Lyft); no significance test; "
                       "revenue = metered fare per engaged hour (tips only recorded for card payments; no driver IDs, so no idle time)"],
    ], [2.4 * cm, W - 2.4 * cm]))

    st.append(Paragraph("8. The FDE judgement call", H))
    st.append(Paragraph(
        "The obvious shortcut is the new <i>cbd_congestion_fee</i> column. But it only exists <b>after</b> the "
        "intervention, so it can't define a 2024 baseline, and any before/after comparison built on it would be "
        "invalid. I defined the CRZ from zone geography instead, as an explicit, challengeable config list applied "
        "identically to both years. I then turned the source's own toll flag into a <b>quality gate</b> that re-tests "
        "the definition on every 2025+ run. If the list is wrong or TLC re-draws zones, the pipeline refuses to publish. "
        "The assumption is visible, tested and enforced, rather than hidden.", B))
    st.append(Spacer(0, 4))
    st.append(Paragraph(f"<b>Repository:</b> <link href='{github}' color='blue'>{github}</link> · "
                        f"<b>Demo:</b> {loom} · Reproduce: <i>pip install -r requirements.txt; "
                        f"python -m pipeline.run</i>", S))
    doc.build(st)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--github", default="https://github.com/AnshMahajan-grep/nyc-crz-taxi-pipeline")
    ap.add_argument("--loom", default="Loom link submitted in the form")
    a = ap.parse_args()
    build(a.github, a.loom)
    print(OUT)
