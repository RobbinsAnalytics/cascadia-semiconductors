"""
conform.py — Turn frozen EDGAR snapshots into tidy, governed quarterly data.

Cascadia Semiconductors · Phase A (FormFactor only)

Input : data/raw/companyfacts_*.json  (frozen by ingest.py — never re-fetched here)
Output: data/conformed/fact_financials_quarterly.csv   (canonical hand-off)
        governance/tag_mapping.csv                     (which XBRL tag fed which metric)

The conformed table is TIDY: one row = company × metric × fiscal quarter,
with precomputed QoQ / YoY variances and explicit governance flags:

  derived   value not filed discretely; computed as FY − (Q1+Q2+Q3)
  restated  a later filing changed this period's value (latest filed wins)
  missing   the quarter grid expects a value and none is filed (never filled)

Everything runs offline from the committed snapshot. GAAP note: all values
are as tagged in XBRL filings and therefore GAAP; company press-release
headline figures are often non-GAAP and will not match.

Usage:
    python src/conform.py
"""

import csv
import json
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
CONFORMED_DIR = REPO_ROOT / "data" / "conformed"
GOVERNANCE_DIR = REPO_ROOT / "governance"

# Analysis window (calendar-conformed quarters, inclusive).
WINDOW_START = (2018, 1)   # FY2018 Q1
WINDOW_END = (2026, 1)     # FY2026 Q1 — latest filed quarter at freeze time

# ---------------------------------------------------------------------------
# Metric definitions and XBRL tag mapping
# ---------------------------------------------------------------------------
# Companies switch XBRL tags over time (e.g. pre/post ASC 606 revenue tags),
# so each metric maps to a PRIORITY LIST of tags: for any given quarter the
# highest-priority tag that has a fact wins, and the winning tag is recorded
# on the row. governance/tag_mapping.csv is generated from this structure so
# the mapping is documented data, not buried code.

FILED_METRICS = {
    # metric_code: (label, unit, [tag priority list])
    "revenue": (
        "Revenue (GAAP)", "USD",
        ["RevenueFromContractWithCustomerExcludingAssessedTax",  # ASC 606, FY2018→
         "SalesRevenueNet",                                      # pre-ASC 606
         "Revenues"],                                            # rare transitional
    ),
    "gross_profit": ("Gross Profit (GAAP)", "USD", ["GrossProfit"]),
    "rnd_expense": ("R&D Expense (GAAP)", "USD", ["ResearchAndDevelopmentExpense"]),
    "sgna_expense": ("SG&A Expense (GAAP)", "USD", ["SellingGeneralAndAdministrativeExpense"]),
    "opex_total": ("Total Operating Expenses (GAAP)", "USD", ["OperatingExpenses"]),
    "operating_income": ("Operating Income (GAAP)", "USD", ["OperatingIncomeLoss"]),
    "net_income": ("Net Income (GAAP)", "USD", ["NetIncomeLoss"]),
    "eps_diluted": ("Diluted EPS (GAAP)", "USD/shares", ["EarningsPerShareDiluted"]),
}

# Ratio metrics computed FROM the conformed filed metrics (not from XBRL
# directly). Variances for these are expressed in percentage points.
COMPUTED_METRICS = {
    "gross_margin_pct": ("Gross Margin % (GAAP)", "pct", "gross_profit / revenue"),
    "opex_ratio_pct": ("Opex as % of Revenue (GAAP)", "pct", "opex_total / revenue"),
    "operating_margin_pct": ("Operating Margin % (GAAP)", "pct", "operating_income / revenue"),
}

# EPS is not strictly additive across quarters (weighted-average share counts
# differ), so a subtraction-derived Q4 EPS is an approximation. It is flagged
# `derived` like every other derived value and footnoted on the page.
NON_ADDITIVE_METRICS = {"eps_diluted"}

# Duration windows (days) used to classify a fact as a discrete quarter vs a
# full fiscal year. 52/53-week calendars give 13- or 14-week quarters
# (91/98 days) and 364/371-day years.
QUARTER_DAYS = (80, 100)
YEAR_DAYS = (350, 380)


# ---------------------------------------------------------------------------
# Fiscal-calendar conformance
# ---------------------------------------------------------------------------

def calendar_quarter_of(end: date):
    """Map a 52/53-week fiscal period end to a calendar (year, quarter).

    Rule (documented in governance/conformance_rules.md): a fiscal quarter is
    assigned to the calendar quarter whose quarter-end date is nearest to the
    fiscal period end, provided they are within 14 days. FormFactor's fiscal
    quarters end within ~6 days of calendar quarter ends, so this is exact in
    practice; anything outside the tolerance raises rather than mislabeling.
    """
    candidates = []
    for year in (end.year - 1, end.year, end.year + 1):
        for q, (month, day) in enumerate([(3, 31), (6, 30), (9, 30), (12, 31)], start=1):
            candidates.append(((year, q), date(year, month, day)))
    (year_q, qend) = min(candidates, key=lambda c: abs((c[1] - end).days))
    if abs((qend - end).days) > 14:
        raise ValueError(f"Period end {end} is >14 days from any calendar quarter end")
    return year_q


def in_window(year: int, quarter: int) -> bool:
    return WINDOW_START <= (year, quarter) <= WINDOW_END


def prev_quarter(year: int, quarter: int):
    return (year - 1, 4) if quarter == 1 else (year, quarter - 1)


# ---------------------------------------------------------------------------
# Fact extraction
# ---------------------------------------------------------------------------

def load_companyfacts(ticker: str) -> dict:
    matches = list(RAW_DIR.glob(f"companyfacts_{ticker}_CIK*.json"))
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one snapshot for {ticker}, found {matches}")
    return json.loads(matches[0].read_text())


def iso(d: str) -> date:
    return date.fromisoformat(d)


def collect_periods(facts: dict, tags: list[str], unit: str):
    """Extract quarterly and full-year facts for one metric.

    Returns two dicts keyed by (year, quarter) / year:
      quarters[(y, q)] = row dict     years[y] = row dict

    Governance applied here:
      * tag priority — a lower-priority tag never overrides a higher one
      * latest filed wins — and if an earlier filing had a DIFFERENT value
        for the same period, the row is marked restated
    """
    quarters: dict = {}
    years: dict = {}

    for priority, tag in enumerate(tags):
        tag_facts = facts.get(tag, {}).get("units", {}).get(unit, [])
        for f in tag_facts:
            if "start" not in f:            # instant facts (balance sheet) — not ours
                continue
            days = (iso(f["end"]) - iso(f["start"])).days
            if QUARTER_DAYS[0] <= days <= QUARTER_DAYS[1]:
                bucket, key = quarters, calendar_quarter_of(iso(f["end"]))
            elif YEAR_DAYS[0] <= days <= YEAR_DAYS[1]:
                year, q = calendar_quarter_of(iso(f["end"]))
                if q != 4:                  # a "year" must end at fiscal year end
                    continue
                bucket, key = years, year
            else:
                continue                    # YTD 6/9-month cumulatives — skip

            row = {
                "value": f["val"],
                "period_start": f["start"],
                "period_end": f["end"],
                "filed": f.get("filed", ""),
                "accn": f.get("accn", ""),
                "form": f.get("form", ""),
                "tag": tag,
                "tag_priority": priority,
                "restated": False,
            }
            existing = bucket.get(key)
            if existing is None:
                bucket[key] = row
            elif priority > existing["tag_priority"]:
                continue                    # never let a fallback tag override
            elif row["filed"] > existing["filed"]:
                row["restated"] = existing["restated"] or (row["value"] != existing["value"])
                bucket[key] = row
            elif row["value"] != existing["value"]:
                existing["restated"] = True  # older filing disagrees → restated
    return quarters, years


def derive_q4(quarters: dict, years: dict, decimals: int | None):
    """Fill missing Q4s as FY − (Q1+Q2+Q3), flagged derived.

    Income-statement concepts usually have no discrete Q4 duration fact
    because the 10-K files the full year. `decimals` controls rounding of the
    derived value (EPS keeps cents; USD metrics stay integral).
    """
    for year, fy in years.items():
        if (year, 4) in quarters:
            continue
        q123 = [quarters.get((year, q)) for q in (1, 2, 3)]
        if any(q is None for q in q123):
            continue                        # can't derive without all three
        val = fy["value"] - sum(q["value"] for q in q123)
        quarters[(year, 4)] = {
            "value": round(val, decimals) if decimals is not None else val,
            "period_start": (iso(quarters[(year, 3)]["period_end"]) + timedelta(days=1)).isoformat(),
            "period_end": fy["period_end"],
            "filed": fy["filed"],
            "accn": fy["accn"],
            "form": fy["form"],
            "tag": fy["tag"],
            "tag_priority": fy["tag_priority"],
            "restated": fy["restated"] or any(q["restated"] for q in q123),
            "derived": True,
        }


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def quarter_grid():
    """Every (year, quarter) in the analysis window, in order."""
    grid, (y, q) = [], WINDOW_START
    while (y, q) <= WINDOW_END:
        grid.append((y, q))
        y, q = (y + 1, 1) if q == 4 else (y, q + 1)
    return grid


def build_company_rows(ticker: str, company: dict, as_of: str):
    """Produce all conformed rows (filed + computed metrics) for one company."""
    facts = load_companyfacts(ticker)["facts"]["us-gaap"]
    grid = quarter_grid()

    # -- filed metrics -----------------------------------------------------
    per_metric: dict[str, dict] = {}
    for code, (label, unit, tags) in FILED_METRICS.items():
        quarters, years = collect_periods(facts, tags, unit)
        derive_q4(quarters, years, decimals=2 if code in NON_ADDITIVE_METRICS else None)
        per_metric[code] = quarters

    # -- computed ratio metrics -------------------------------------------
    ratio_parts = {
        "gross_margin_pct": ("gross_profit", "revenue"),
        "opex_ratio_pct": ("opex_total", "revenue"),
        "operating_margin_pct": ("operating_income", "revenue"),
    }
    for code, (num_code, den_code) in ratio_parts.items():
        quarters = {}
        for key in grid:
            num, den = per_metric[num_code].get(key), per_metric[den_code].get(key)
            if num is None or den is None or not den["value"]:
                continue
            quarters[key] = {
                "value": round(100.0 * num["value"] / den["value"], 2),
                "period_start": num["period_start"],
                "period_end": num["period_end"],
                "filed": max(num["filed"], den["filed"]),
                "accn": "", "form": "",
                "tag": f"computed: {COMPUTED_METRICS[code][2]}",
                "restated": num["restated"] or den["restated"],
                "derived": num.get("derived", False) or den.get("derived", False),
            }
        per_metric[code] = quarters

    # -- flatten to tidy rows with variances -------------------------------
    all_metrics = {**{k: (v[0], v[1]) for k, v in FILED_METRICS.items()},
                   **{k: (v[0], v[1]) for k, v in COMPUTED_METRICS.items()}}
    rows = []
    for code, (label, unit) in all_metrics.items():
        quarters = per_metric[code]
        is_pct = unit == "pct"
        for (year, q) in grid:
            r = quarters.get((year, q))
            base = {
                "ticker": ticker, "company": company["entity_name"],
                "metric_code": code, "metric_label": label, "unit": unit,
                "fiscal_year": year, "fiscal_quarter": f"Q{q}",
                "fiscal_period": f"{year}Q{q}", "as_of_date": as_of,
            }
            if r is None:
                # Missing is a first-class value: the row exists, flagged.
                rows.append({**base, "value": "", "missing": True, "derived": False,
                             "restated": False, "period_start": "", "period_end": "",
                             "xbrl_tag": "", "source_form": "", "accn": "", "filed": "",
                             "qoq_delta": "", "qoq_pct": "", "yoy_delta": "", "yoy_pct": ""})
                continue

            def variance(prior_key):
                prior = quarters.get(prior_key)
                if prior is None:
                    return "", ""
                delta = round(r["value"] - prior["value"], 4)
                if is_pct:                  # margin changes are pct-POINT deltas only
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
    return rows


def write_tag_mapping(as_of: str):
    """Emit the governance tag-mapping table (metric × tag × role × notes)."""
    GOVERNANCE_DIR.mkdir(exist_ok=True)
    with open(GOVERNANCE_DIR / "tag_mapping.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ticker", "metric_code", "metric_label", "xbrl_tag", "role", "notes", "as_of_date"])
        for code, (label, unit, tags) in FILED_METRICS.items():
            for i, tag in enumerate(tags):
                role = "primary" if i == 0 else f"fallback_{i}"
                note = ""
                if code == "revenue" and i == 0:
                    note = "ASC 606 tag; covers FY2018 onward for FORM"
                elif code == "revenue" and tag == "SalesRevenueNet":
                    note = "pre-ASC 606 tag used by FORM through FY2018 transition"
                w.writerow(["FORM", code, label, tag, role, note, as_of])
        for code, (label, unit, formula) in COMPUTED_METRICS.items():
            w.writerow(["FORM", code, label, f"computed: {formula}", "computed",
                        "ratio computed from conformed GAAP metrics", as_of])


def main() -> None:
    manifest = json.loads((RAW_DIR / "manifest.json").read_text())
    as_of = manifest["as_of_date"]
    CONFORMED_DIR.mkdir(parents=True, exist_ok=True)

    all_rows = []
    for ticker, company in manifest["companies"].items():
        rows = build_company_rows(ticker, company, as_of)
        all_rows.extend(rows)
        n_missing = sum(1 for r in rows if r["missing"])
        n_derived = sum(1 for r in rows if r["derived"])
        n_restated = sum(1 for r in rows if r["restated"])
        print(f"{ticker}: {len(rows)} rows | derived={n_derived} restated={n_restated} missing={n_missing}")

    out = CONFORMED_DIR / "fact_financials_quarterly.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        w.writerows(all_rows)
    print(f"Wrote {out} ({len(all_rows)} rows), as_of_date = {as_of}")

    write_tag_mapping(as_of)
    print(f"Wrote {GOVERNANCE_DIR / 'tag_mapping.csv'}")


if __name__ == "__main__":
    main()
