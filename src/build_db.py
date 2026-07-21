"""
build_db.py — Load the conformed CSV into a SQLite star schema.

Cascadia Semiconductors · Phase A

Input : data/conformed/fact_financials_quarterly.csv
Output: data/cascadia_semi.db

Schema (dimensional model, mirroring the other Cascadia builds):

    dim_company   one row per SEC filer (ticker, CIK, entity name)
    dim_metric    one row per metric (label, unit, GAAP basis, formula if computed)
    dim_date      one row per calendar-conformed fiscal quarter
    fact_financials_quarterly
                  grain: company × metric × quarter, with value, QoQ/YoY
                  variances, and governance flags (derived/restated/missing)

The database is a convenience mirror of the CSV — the CSV remains the
canonical hand-off. Rebuilding is idempotent: tables are dropped and
recreated from the CSV every run.

Usage:
    python src/build_db.py
"""

import csv
import json
import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "data" / "conformed" / "fact_financials_quarterly.csv"
DB_PATH = REPO_ROOT / "data" / "cascadia_semi.db"
MANIFEST = REPO_ROOT / "data" / "raw" / "manifest.json"

DDL = """
DROP TABLE IF EXISTS fact_financials_quarterly;
DROP TABLE IF EXISTS dim_company;
DROP TABLE IF EXISTS dim_metric;
DROP TABLE IF EXISTS dim_date;

CREATE TABLE dim_company (
    company_key   INTEGER PRIMARY KEY,
    ticker        TEXT NOT NULL UNIQUE,
    cik           TEXT NOT NULL,          -- zero-padded 10-digit SEC CIK
    entity_name   TEXT NOT NULL
);

CREATE TABLE dim_metric (
    metric_key    INTEGER PRIMARY KEY,
    metric_code   TEXT NOT NULL UNIQUE,
    metric_label  TEXT NOT NULL,
    unit          TEXT NOT NULL,          -- USD | USD/shares | pct
    basis         TEXT NOT NULL DEFAULT 'GAAP per XBRL filings'
);

CREATE TABLE dim_date (
    date_key       INTEGER PRIMARY KEY,   -- e.g. 20261 = 2026 Q1
    fiscal_year    INTEGER NOT NULL,
    fiscal_quarter TEXT NOT NULL,         -- Q1..Q4 (calendar-conformed)
    fiscal_period  TEXT NOT NULL UNIQUE   -- e.g. '2026Q1'
);

CREATE TABLE fact_financials_quarterly (
    company_key   INTEGER NOT NULL REFERENCES dim_company(company_key),
    metric_key    INTEGER NOT NULL REFERENCES dim_metric(metric_key),
    date_key      INTEGER NOT NULL REFERENCES dim_date(date_key),
    value         REAL,                   -- NULL when missing (never zero-filled)
    qoq_delta     REAL,
    qoq_pct       REAL,
    yoy_delta     REAL,
    yoy_pct       REAL,
    derived       INTEGER NOT NULL,       -- 1 = FY − (Q1+Q2+Q3), not discretely filed
    restated      INTEGER NOT NULL,       -- 1 = a later filing changed this value
    missing       INTEGER NOT NULL,       -- 1 = not disclosed; value is NULL
    period_start  TEXT,
    period_end    TEXT,
    xbrl_tag      TEXT,
    source_form   TEXT,                   -- 10-Q / 10-K
    accn          TEXT,                   -- SEC accession number (audit trail)
    filed         TEXT,                   -- filing date of the winning fact
    as_of_date    TEXT NOT NULL,          -- snapshot freeze date
    PRIMARY KEY (company_key, metric_key, date_key)
);
"""


def num(s: str):
    """CSV blank → NULL; anything else → float."""
    return float(s) if s not in ("", None) else None


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))

    con = sqlite3.connect(DB_PATH)
    con.executescript(DDL)

    # --- dimensions, keyed deterministically from the conformed data -------
    companies = {}
    for r in rows:
        if r["ticker"] not in companies:
            cik = manifest["companies"][r["ticker"]]["cik"]
            companies[r["ticker"]] = (len(companies) + 1, r["ticker"], cik, r["company"])
    con.executemany("INSERT INTO dim_company VALUES (?,?,?,?)", companies.values())

    metrics = {}
    for r in rows:
        if r["metric_code"] not in metrics:
            metrics[r["metric_code"]] = (len(metrics) + 1, r["metric_code"],
                                         r["metric_label"], r["unit"],
                                         "GAAP per XBRL filings")
    con.executemany("INSERT INTO dim_metric VALUES (?,?,?,?,?)", metrics.values())

    dates = {}
    for r in rows:
        if r["fiscal_period"] not in dates:
            key = int(r["fiscal_year"]) * 10 + int(r["fiscal_quarter"][1])
            dates[r["fiscal_period"]] = (key, int(r["fiscal_year"]),
                                         r["fiscal_quarter"], r["fiscal_period"])
    con.executemany("INSERT INTO dim_date VALUES (?,?,?,?)", dates.values())

    # --- fact table --------------------------------------------------------
    con.executemany(
        "INSERT INTO fact_financials_quarterly VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(companies[r["ticker"]][0], metrics[r["metric_code"]][0],
          dates[r["fiscal_period"]][0], num(r["value"]),
          num(r["qoq_delta"]), num(r["qoq_pct"]), num(r["yoy_delta"]), num(r["yoy_pct"]),
          int(r["derived"] == "True"), int(r["restated"] == "True"),
          int(r["missing"] == "True"), r["period_start"] or None,
          r["period_end"] or None, r["xbrl_tag"] or None, r["source_form"] or None,
          r["accn"] or None, r["filed"] or None, r["as_of_date"])
         for r in rows])

    con.commit()
    counts = {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("dim_company", "dim_metric", "dim_date", "fact_financials_quarterly")}
    con.close()
    print(f"Wrote {DB_PATH}")
    for t, n in counts.items():
        print(f"  {t}: {n} rows")


if __name__ == "__main__":
    main()
