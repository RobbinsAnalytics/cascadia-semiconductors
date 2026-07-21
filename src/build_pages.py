"""
build_pages.py — Package the conformed data for the static frontend.

Cascadia Semiconductors · Phase A

Input : data/conformed/fact_financials_quarterly.csv
Output: docs/data/form_quarterly.json      (readable hand-off copy)
        docs/index.html                    (data INJECTED inline between markers)

Why inline? The GitHub Pages site must work forever, free, and offline —
including opened straight from disk (file://), where relative fetch() is
blocked by browsers. So the frozen JSON is embedded in the HTML between
  <!--CASCADIA_DATA_START--> ... <!--CASCADIA_DATA_END-->
markers. This script is idempotent: it replaces whatever is between the
markers, so re-running after an HTML edit is always safe.

No network access. No SEC calls. The page renders the freeze, nothing else.

Usage:
    python src/build_pages.py
"""

import csv
import json
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CSV_PATH = REPO_ROOT / "data" / "conformed" / "fact_financials_quarterly.csv"
DOCS_DIR = REPO_ROOT / "docs"
JSON_OUT = DOCS_DIR / "data" / "form_quarterly.json"
PAGES = [DOCS_DIR / "index.html"]          # Phases B/C append their pages here

MARK_START = "<!--CASCADIA_DATA_START-->"
MARK_END = "<!--CASCADIA_DATA_END-->"

# USD metrics are shipped to the page in $ millions (2 decimals) — the page
# never rescales; EPS and pct metrics ship as filed/computed.
USD_TO_MILLIONS = 1e-6


def opt(v: str, scale: float = 1.0, nd: int = 2):
    """CSV cell → rounded float, or None for blank (missing stays null)."""
    return round(float(v) * scale, nd) if v not in ("", None) else None


def build_payload() -> dict:
    rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    periods = sorted({r["fiscal_period"] for r in rows})
    idx = {p: i for i, p in enumerate(periods)}

    metrics: dict = defaultdict(lambda: {
        "values": [None] * len(periods), "qoq_delta": [None] * len(periods),
        "qoq_pct": [None] * len(periods), "yoy_delta": [None] * len(periods),
        "yoy_pct": [None] * len(periods), "derived": [False] * len(periods),
        "restated": [False] * len(periods), "missing": [False] * len(periods),
    })
    for r in rows:
        m, i = metrics[r["metric_code"]], idx[r["fiscal_period"]]
        m["label"], m["unit"] = r["metric_label"], r["unit"]
        usd = r["unit"] == "USD"
        scale = USD_TO_MILLIONS if usd else 1.0
        m["values"][i] = opt(r["value"], scale, 2 if not usd else 2)
        m["qoq_delta"][i] = opt(r["qoq_delta"], scale)
        m["qoq_pct"][i] = opt(r["qoq_pct"])
        m["yoy_delta"][i] = opt(r["yoy_delta"], scale)
        m["yoy_pct"][i] = opt(r["yoy_pct"])
        m["derived"][i] = r["derived"] == "True"
        m["restated"][i] = r["restated"] == "True"
        m["missing"][i] = r["missing"] == "True"

    first = rows[0]
    return {
        "as_of_date": first["as_of_date"],
        "ticker": first["ticker"],
        "company": first["company"],
        "basis": "GAAP per XBRL filings; company headline figures may be non-GAAP",
        "usd_unit": "USD millions",
        "periods": periods,
        "latest_period": periods[-1],
        "metrics": dict(metrics),
    }


def inject(page: Path, payload_json: str) -> None:
    html = page.read_text(encoding="utf-8")
    start, end = html.index(MARK_START), html.index(MARK_END)
    block = (f"{MARK_START}\n<script id=\"cascadia-data\" "
             f"type=\"application/json\">\n{payload_json}\n</script>\n")
    page.write_text(html[:start] + block + html[end:], encoding="utf-8")


def main() -> None:
    payload = build_payload()
    payload_json = json.dumps(payload, separators=(",", ":"))

    JSON_OUT.parent.mkdir(parents=True, exist_ok=True)
    JSON_OUT.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    print(f"Wrote {JSON_OUT} ({len(payload['periods'])} quarters, "
          f"as-of {payload['as_of_date']})")

    for page in PAGES:
        inject(page, payload_json)
        print(f"Injected data into {page}")


if __name__ == "__main__":
    main()
