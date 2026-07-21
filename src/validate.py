"""
validate.py — Reconcile and sanity-check the conformed data; write the report.

Cascadia Semiconductors · Phase A

Checks performed (all offline, against the frozen snapshot):

  1. Q4 / full-year reconciliation — for every fiscal year with a filed FY
     fact, Q1+Q2+Q3+Q4 must equal FY within tolerance (USD metrics: $2,000
     absolute — XBRL values are filed as exact integers; EPS: $0.02, since
     per-share amounts are not strictly additive across quarters).
  2. Cross-foot consistency — Gross Profit − Total Opex + Gain on Sale of
     Business must equal Operating Income each quarter (same tolerances).
     The gain term matters: FormFactor's GAAP operating income includes
     divestiture gains (FRT Metrology, FY2023 Q4: +$72.9M; China business,
     FY2024 Q1: +$20.3M) that sit outside the OperatingExpenses tag. When no
     gain fact is filed for a quarter the term is zero BY ACCOUNTING IDENTITY
     (no such line item on that income statement) — this is not zero-filling
     a missing disclosure. This check catches tag-mapping errors.
  3. Gap audit — every quarter in the FY2018→FY2026Q1 grid must either carry
     a value or be explicitly flagged missing. No silent gaps.
  4. Flag audit — every derived and restated row is enumerated for review.
  5. Spot-check table — recent quarters' GAAP revenue and gross profit are
     listed with their source filings so a human can verify them against
     FormFactor's press releases (GAAP tables, not non-GAAP headlines).

Output: governance/validation_report.md  (regenerated every run)
Exit code is non-zero if any check FAILS, so this can gate a rebuild.

Usage:
    python src/validate.py
"""

import csv
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

# Reuse the conform layer's extraction logic so validation sees the same
# facts the pipeline used — no second, subtly-different parser.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from conform import (FILED_METRICS, NON_ADDITIVE_METRICS, collect_periods,
                     load_companyfacts)

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "data" / "conformed" / "fact_financials_quarterly.csv"
REPORT_PATH = REPO_ROOT / "governance" / "validation_report.md"
MANIFEST = REPO_ROOT / "data" / "raw" / "manifest.json"

USD_TOLERANCE = 2_000     # dollars; filings are exact integers, so this is generous
EPS_TOLERANCE = 0.02      # dollars/share; EPS is not strictly additive


def load_conformed():
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    by_metric = defaultdict(dict)
    for r in rows:
        by_metric[r["metric_code"]][r["fiscal_period"]] = r
    return rows, by_metric


def check_fy_reconciliation(results):
    """Check 1: quarters sum to the filed full-year value."""
    facts = load_companyfacts("FORM")["facts"]["us-gaap"]
    _, by_metric = load_conformed()
    failures, checked = [], 0
    for code, (label, unit, tags) in FILED_METRICS.items():
        tol = EPS_TOLERANCE if code in NON_ADDITIVE_METRICS else USD_TOLERANCE
        _, years = collect_periods(facts, tags, unit)
        for year, fy in sorted(years.items()):
            quarters = [by_metric[code].get(f"{year}Q{q}") for q in (1, 2, 3, 4)]
            if any(q is None or q["missing"] == "True" for q in quarters):
                continue  # outside the analysis window or genuinely missing
            qsum = sum(float(q["value"]) for q in quarters)
            checked += 1
            if abs(qsum - fy["value"]) > tol:
                failures.append(f"{code} FY{year}: quarters sum {qsum:,.2f} "
                                f"vs FY filed {fy['value']:,.2f}")
    results.append(("Q4 / full-year reconciliation",
                    not failures, f"{checked} metric-years reconciled", failures))


def check_crossfoot(results):
    """Check 2: gross_profit − opex_total + gain_on_sale == operating_income.

    GainLossOnSaleOfBusiness is extracted from the same frozen snapshot with
    the same quarter/Q4-derivation logic as every other metric. Quarters with
    no gain fact use zero — an accounting identity (no such income-statement
    line that quarter), not a fill of missing data.
    """
    facts = load_companyfacts("FORM")["facts"]["us-gaap"]
    gains_q, gains_fy = collect_periods(facts, ["GainLossOnSaleOfBusiness"], "USD")
    # Q4 gain = FY − (Q1+Q2+Q3), where quarters with no gain fact contribute
    # zero (the line item didn't exist those quarters — identity, not a fill).
    for year, fy in gains_fy.items():
        if (year, 4) not in gains_q:
            q123 = sum(gains_q.get((year, q), {}).get("value", 0) for q in (1, 2, 3))
            gains_q[(year, 4)] = {"value": fy["value"] - q123}

    _, by_metric = load_conformed()
    failures, checked = [], 0
    for period, gp in by_metric["gross_profit"].items():
        opex = by_metric["opex_total"].get(period)
        oi = by_metric["operating_income"].get(period)
        if any(x is None or x["missing"] == "True" for x in (gp, opex, oi)):
            continue
        checked += 1
        key = (int(period[:4]), int(period[-1]))
        gain = gains_q.get(key, {}).get("value", 0)
        diff = float(gp["value"]) - float(opex["value"]) + gain - float(oi["value"])
        if abs(diff) > USD_TOLERANCE:
            failures.append(f"{period}: GP−Opex+Gain−OI = {diff:,.0f}")
    results.append(("Cross-foot (GP − Opex + Divestiture Gains = Operating Income)",
                    not failures, f"{checked} quarters checked", failures))


def check_gaps(results):
    """Check 3: no silent gaps — every grid cell has a value or a missing flag."""
    rows, _ = load_conformed()
    bad = [f"{r['metric_code']} {r['fiscal_period']}" for r in rows
           if r["value"] == "" and r["missing"] != "True"]
    n_missing = sum(1 for r in rows if r["missing"] == "True")
    results.append(("Gap audit (missing is flagged, never silent)",
                    not bad, f"{len(rows)} rows scanned; {n_missing} flagged missing", bad))


def enumerate_flags():
    """Check 4 (informational): list every derived / restated row."""
    rows, _ = load_conformed()
    derived = [r for r in rows if r["derived"] == "True" and r["unit"] != "pct"]
    restated = [r for r in rows if r["restated"] == "True" and r["unit"] != "pct"]
    return derived, restated


def spot_check_table():
    """Check 5: recent revenue / gross profit values for human verification."""
    _, by_metric = load_conformed()
    picks = ["2026Q1", "2025Q4", "2025Q3", "2025Q2", "2025Q1"]
    lines = []
    for period in picks:
        rev, gp = by_metric["revenue"].get(period), by_metric["gross_profit"].get(period)
        if not rev or rev["missing"] == "True":
            continue
        note = "derived: FY − (Q1+Q2+Q3)" if rev["derived"] == "True" else \
               f"{rev['source_form']} filed {rev['filed']}"
        lines.append((period, float(rev["value"]), float(gp["value"]),
                      round(100 * float(gp["value"]) / float(rev["value"]), 1), note))
    return lines


def main() -> None:
    as_of = json.loads(MANIFEST.read_text())["as_of_date"]
    results = []
    check_fy_reconciliation(results)
    check_crossfoot(results)
    check_gaps(results)
    derived, restated = enumerate_flags()
    spots = spot_check_table()

    all_pass = all(ok for _, ok, _, _ in results)

    md = [
        "# Validation Report — Cascadia Semiconductors",
        "",
        f"*Auto-generated by `src/validate.py` on {date.today().isoformat()}. "
        f"Data snapshot as-of **{as_of}** (SEC EDGAR XBRL, frozen in `data/raw/`).*",
        "",
        f"**Overall: {'ALL CHECKS PASS' if all_pass else 'FAILURES DETECTED'}**",
        "",
        "## Automated checks",
        "",
        "| # | Check | Result | Coverage |",
        "|---|-------|--------|----------|",
    ]
    for i, (name, ok, coverage, failures) in enumerate(results, 1):
        md.append(f"| {i} | {name} | {'PASS' if ok else 'FAIL'} | {coverage} |")
    for name, ok, _, failures in results:
        if failures:
            md += ["", f"### Failures — {name}", ""] + [f"- {f}" for f in failures]

    md += [
        "",
        "## Spot-check table (verify against FormFactor GAAP press-release figures)",
        "",
        "These are the GAAP values this pipeline extracted. Compare them to the",
        "*GAAP* income-statement tables in FormFactor's earnings press releases",
        "(investors.formfactor.com) — **not** the non-GAAP headline numbers.",
        "",
        "| Quarter | Revenue (GAAP) | Gross Profit (GAAP) | GM% (GAAP) | Source |",
        "|---------|----------------|---------------------|------------|--------|",
    ]
    for period, rev, gp, gm, note in spots:
        md.append(f"| {period} | ${rev/1e6:,.1f}M | ${gp/1e6:,.1f}M | {gm}% | {note} |")

    md += [
        "",
        "## Governance flags",
        "",
        f"**Derived values ({len(derived)} rows):** Q4 income-statement values are",
        "not filed discretely (the 10-K files the full year), so Q4 = FY − (Q1+Q2+Q3).",
        "Diluted EPS derived this way is approximate — per-share amounts are not",
        "strictly additive when share counts move between quarters.",
        "",
        "| Metric | Periods derived |",
        "|--------|-----------------|",
    ]
    by_code = defaultdict(list)
    for r in derived:
        by_code[r["metric_code"]].append(r["fiscal_period"])
    for code, periods in sorted(by_code.items()):
        md.append(f"| {code} | {', '.join(sorted(periods))} |")

    md += ["", f"**Restated values ({len(restated)} rows):** latest filed value wins.", ""]
    if restated:
        md += ["| Metric | Period | Value kept | Filed |", "|--------|--------|-----------|-------|"]
        md += [f"| {r['metric_code']} | {r['fiscal_period']} | {float(r['value']):,.0f} | "
               f"{r['filed']} ({r['source_form']}) |" for r in restated]
    else:
        md.append("None detected.")

    md += [
        "",
        "---",
        "*GAAP per XBRL filings; company headline figures may be non-GAAP.",
        "Missing data is flagged, never filled. Built from public SEC filings",
        f"(EDGAR XBRL APIs), as-of {as_of}. Not investment advice.*",
        "",
    ]

    REPORT_PATH.write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {REPORT_PATH}")
    for name, ok, coverage, _ in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} — {coverage}")
    if not all_pass:
        sys.exit(1)


if __name__ == "__main__":
    main()
