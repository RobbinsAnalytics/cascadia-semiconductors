"""
conform_segments.py — Govern FormFactor's segment facts into tidy quarterly data.

Cascadia Semiconductors · Phase B (FormFactor segments)

Input : data/raw/segments_FORM_CIK0001039399.json   (frozen by ingest_segments.py)
Output: data/conformed/fact_segments_quarterly.csv   (canonical segment hand-off)

This is the segment sibling of conform.py and reuses its governance helpers so
segments obey EXACTLY the same rules as the consolidated series:

  * fiscal-calendar conformance (52/53-week -> nearest calendar quarter)
  * period classification by duration (quarter vs full year; YTD ignored)
  * tag priority is trivial here (one tag per metric) but latest-filed-wins and
    the `restated` flag work identically
  * derived Q4 = FY - (Q1+Q2+Q3), flagged `derived`
  * missing is flagged, never filled

Metrics conformed, per reportable segment (Probe Cards, Systems):
  seg_revenue          RevenueFromContractWithCustomerExcludingAssessedTax
  seg_gross_profit     GrossProfit   (segment measure — see note below)
  seg_gross_margin_pct computed = seg_gross_profit / seg_revenue

GOVERNANCE NOTE (carried onto the page): segment REVENUE reconciles to
consolidated revenue exactly (validate.py check). Segment GROSS PROFIT is
FormFactor's segment measure of profit and EXCLUDES unallocated cost-of-revenue
items (stock-based compensation, amortization of acquisition intangibles), so
segment gross profit does NOT sum to consolidated GAAP gross profit. Segment
OPERATING INCOME is not disclosed (operating expenses are largely unallocated),
so it is not produced here — it is surfaced on the page as "not disclosed."

Usage:
    python src/conform_segments.py
"""

import csv
import json
from pathlib import Path

# Reuse the Phase A conform layer so segments and consolidated share one set of
# rules (no second, subtly-different implementation).
from conform import (calendar_quarter_of, iso, prev_quarter,
                     quarter_grid, QUARTER_DAYS, YEAR_DAYS)

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
CONFORMED_DIR = REPO_ROOT / "data" / "conformed"
GOVERNANCE_DIR = REPO_ROOT / "governance"

SEG_RAW = RAW_DIR / "segments_FORM_CIK0001039399.json"
OUT_CSV = CONFORMED_DIR / "fact_segments_quarterly.csv"

SEGMENTS = {"probe_cards": "Probe Cards", "systems": "Systems"}
CORP = "corporate_unallocated"          # the filed reconciling line (ASU 2023-07+)
CORP_LABEL = "Corporate / unallocated (reconciling)"

# metric_code: (label, unit, source XBRL tag)
FILED_SEG_METRICS = {
    "seg_revenue": ("Segment Revenue (GAAP)", "USD",
                    "RevenueFromContractWithCustomerExcludingAssessedTax"),
    "seg_gross_profit": ("Segment Gross Profit (segment measure)", "USD", "GrossProfit"),
}
COMPUTED_SEG_METRICS = {
    "seg_gross_margin_pct": ("Segment Gross Margin % (segment measure)", "pct",
                             "seg_gross_profit / seg_revenue"),
}

# GOVERNANCE GATE for segment gross profit. FormFactor discloses a segment
# gross-profit measure across the whole window, BUT only from FY2024 (FASB ASU
# 2023-07) does it also file the CorporateNonSegment reconciling line that makes
# the segment gross-profit bridge close EXACTLY to consolidated GAAP gross
# profit. Before that, segment gross profit sums to MORE than consolidated GP
# and the reconciling item is NOT tagged, so it cannot be tied to GAAP. We
# therefore emit segment gross profit / margin ONLY for periods that carry a
# filed corporate-unallocated gross-profit fact; earlier periods are flagged
# missing ("not reconcilable from tagged XBRL"). This is the honest boundary.
GP_GATE_TAG = "GrossProfit"


def collect_segment_periods(facts, segment, tag):
    """Quarter/year facts for one (segment, tag), latest-filed-wins + restated.

    Mirrors conform.collect_periods, but keyed by the segment we already
    resolved in ingest_segments.py. Only one XBRL tag feeds each segment metric,
    so there is no tag-priority contest — just period classification and the
    latest-filed / restated logic.
    """
    quarters, years = {}, {}
    for f in facts:
        if f["segment"] != segment or f["tag"] != tag:
            continue
        days = (iso(f["period_end"]) - iso(f["period_start"])).days
        if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
            bucket, key = quarters, calendar_quarter_of(iso(f["period_end"]))
        elif YEAR_DAYS[0] <= days <= YEAR_DAYS[1]:
            year, q = calendar_quarter_of(iso(f["period_end"]))
            if q != 4:                       # a "year" must end at fiscal year end
                continue
            bucket, key = years, year
        else:
            continue                         # 6-/9-month YTD cumulatives — skip
        row = {"value": f["value"], "period_start": f["period_start"],
               "period_end": f["period_end"], "filed": f["filed"],
               "accn": f["accn"], "form": f["form"], "tag": f["tag"],
               "restated": False}
        existing = bucket.get(key)
        if existing is None:
            bucket[key] = row
        elif row["filed"] > existing["filed"]:
            row["restated"] = existing["restated"] or (row["value"] != existing["value"])
            bucket[key] = row
        elif row["value"] != existing["value"]:
            existing["restated"] = True
    return quarters, years


def derive_q4(quarters, years):
    """Fill missing Q4 as FY - (Q1+Q2+Q3), flagged derived (USD segment metrics)."""
    for year, fy in years.items():
        if (year, 4) in quarters:
            continue
        q123 = [quarters.get((year, q)) for q in (1, 2, 3)]
        if any(q is None for q in q123):
            continue
        quarters[(year, 4)] = {
            "value": fy["value"] - sum(q["value"] for q in q123),
            "period_start": "", "period_end": fy["period_end"],
            "filed": fy["filed"], "accn": fy["accn"], "form": fy["form"],
            "tag": fy["tag"],
            "restated": fy["restated"] or any(q["restated"] for q in q123),
            "derived": True,
        }


def series_for(facts, seg_code):
    """Build {metric_code: quarters} for one bucket, with Q4 derived."""
    per_metric = {}
    for code, (label, unit, tag) in FILED_SEG_METRICS.items():
        quarters, years = collect_segment_periods(facts, seg_code, tag)
        derive_q4(quarters, years)
        per_metric[code] = quarters
    return per_metric


def emit_row(rows, base, quarters, key, is_pct):
    """Append one tidy row (value or missing) with QoQ/YoY variances."""
    year, q = key
    r = quarters.get(key)
    if r is None:
        rows.append({**base, "value": "", "missing": True, "derived": False,
                     "restated": False, "period_start": "", "period_end": "",
                     "xbrl_tag": "", "source_form": "", "accn": "", "filed": "",
                     "qoq_delta": "", "qoq_pct": "", "yoy_delta": "", "yoy_pct": ""})
        return

    def variance(prior_key):
        prior = quarters.get(prior_key)
        if prior is None:
            return "", ""
        delta = round(r["value"] - prior["value"], 4)
        if is_pct:                         # margin deltas are percentage POINTS
            return delta, ""
        pct = round(100.0 * delta / abs(prior["value"]), 2) if prior["value"] else ""
        return delta, pct

    qoq_delta, qoq_pct = variance(prev_quarter(year, q))
    yoy_delta, yoy_pct = variance((year - 1, q))
    rows.append({**base, "value": r["value"], "missing": False,
                 "derived": r.get("derived", False), "restated": r["restated"],
                 "period_start": r["period_start"], "period_end": r["period_end"],
                 "xbrl_tag": r["tag"], "source_form": r["form"], "accn": r["accn"],
                 "filed": r["filed"], "qoq_delta": qoq_delta, "qoq_pct": qoq_pct,
                 "yoy_delta": yoy_delta, "yoy_pct": yoy_pct})


def build_rows(company: str, as_of: str):
    payload = json.loads(SEG_RAW.read_text(encoding="utf-8"))
    facts = payload["facts"]
    grid = quarter_grid()
    rows = []

    # Per-bucket filed series (revenue + gross profit), Q4 derived.
    series = {seg: series_for(facts, seg) for seg in (*SEGMENTS, CORP)}

    # The reconcilable gross-profit window = periods with a FILED corporate
    # reconciling gross-profit fact (incl. derived Q4). Segment GP/margin are
    # emitted ONLY here; everywhere else they are flagged missing.
    gp_window = set(series[CORP]["seg_gross_profit"].keys())

    def base_of(seg_code, seg_label, code, label, unit, key):
        return {
            "ticker": "FORM", "company": company,
            "segment_code": seg_code, "segment_label": seg_label,
            "metric_code": code, "metric_label": label, "unit": unit,
            "fiscal_year": key[0], "fiscal_quarter": f"Q{key[1]}",
            "fiscal_period": f"{key[0]}Q{key[1]}", "as_of_date": as_of,
        }

    # -- the two reportable segments: revenue (full), gross profit + margin (gated)
    for seg_code, seg_label in SEGMENTS.items():
        rev = series[seg_code]["seg_revenue"]
        gp_full = series[seg_code]["seg_gross_profit"]
        # gate gross profit to the reconcilable window
        gp = {k: v for k, v in gp_full.items() if k in gp_window}
        # computed margin only where gated gross profit AND revenue exist
        gm = {}
        for key in grid:
            r, g = rev.get(key), gp.get(key)
            if r and g and r["value"]:
                gm[key] = {"value": round(100.0 * g["value"] / r["value"], 2),
                           "period_start": g["period_start"], "period_end": g["period_end"],
                           "filed": max(g["filed"], r["filed"]), "accn": "", "form": "",
                           "tag": f"computed: {COMPUTED_SEG_METRICS['seg_gross_margin_pct'][2]}",
                           "restated": g["restated"] or r["restated"],
                           "derived": g.get("derived", False) or r.get("derived", False)}
        emit = {"seg_revenue": rev, "seg_gross_profit": gp, "seg_gross_margin_pct": gm}
        meta = {**{k: (v[0], v[1]) for k, v in FILED_SEG_METRICS.items()},
                **{k: (v[0], v[1]) for k, v in COMPUTED_SEG_METRICS.items()}}
        for code, (label, unit) in meta.items():
            for key in grid:
                emit_row(rows, base_of(seg_code, seg_label, code, label, unit, key),
                         emit[code], key, unit == "pct")

    # -- corporate / unallocated reconciling line: gross profit only, only where
    #    filed (no full grid — it is a bridge artifact, not a headline series).
    label, unit = FILED_SEG_METRICS["seg_gross_profit"][0], FILED_SEG_METRICS["seg_gross_profit"][1]
    corp_gp = series[CORP]["seg_gross_profit"]
    for key in sorted(corp_gp):
        emit_row(rows, base_of(CORP, CORP_LABEL, "seg_gross_profit", label, unit, key),
                 corp_gp, key, False)
    return rows


def append_tag_mapping(as_of: str):
    """Append the segment concepts to governance/tag_mapping.csv (idempotent).

    conform.py rewrites tag_mapping.csv from scratch for the consolidated
    metrics; we APPEND the segment rows if they are not already present, so the
    one governance file documents every concept the project uses.
    """
    path = GOVERNANCE_DIR / "tag_mapping.csv"
    existing = list(csv.reader(open(path, encoding="utf-8"))) if path.exists() else []
    have = {(r[1], r[3]) for r in existing[1:]} if existing else set()
    new = []
    for code, (label, unit, tag) in FILED_SEG_METRICS.items():
        if (code, tag) not in have:
            note = ("segment-dimensioned fact (us-gaap:StatementBusinessSegmentsAxis); "
                    "reconciles to consolidated revenue" if code == "seg_revenue" else
                    "segment measure of profit; EXCLUDES unallocated COGS — does NOT "
                    "sum to consolidated GAAP gross profit")
            new.append(["FORM", code, label, tag, "primary_segment", note, as_of])
    for code, (label, unit, formula) in COMPUTED_SEG_METRICS.items():
        tagstr = f"computed: {formula}"
        if (code, tagstr) not in have:
            new.append(["FORM", code, label, tagstr, "computed_segment",
                        "ratio computed from conformed segment metrics", as_of])
    if new:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(new)
    return len(new)


def main() -> None:
    manifest = json.loads((RAW_DIR / "manifest.json").read_text())
    as_of = manifest["as_of_date"]
    company = manifest["companies"]["FORM"]["entity_name"]
    CONFORMED_DIR.mkdir(parents=True, exist_ok=True)

    rows = build_rows(company, as_of)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    n_missing = sum(1 for r in rows if r["missing"])
    n_derived = sum(1 for r in rows if r["derived"])
    n_restated = sum(1 for r in rows if r["restated"])
    print(f"Wrote {OUT_CSV} ({len(rows)} rows | "
          f"derived={n_derived} restated={n_restated} missing={n_missing}), as-of {as_of}")
    added = append_tag_mapping(as_of)
    print(f"tag_mapping.csv: appended {added} segment concept rows")


if __name__ == "__main__":
    main()
