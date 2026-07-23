"""
conform_peers.py — Govern the peer companyfacts into tidy quarterly data.

Cascadia Semiconductors · Phase 3 (peer benchmark)

Input : data/raw/companyfacts_<PEER>_CIK*.json  (frozen by ingest_peers.py)
        data/raw/peers_manifest.json
Output: data/conformed/fact_peers_quarterly.csv  (one row per peer × metric × quarter)

This is the peer sibling of conform.py and REUSES its `build_company_rows` so
every peer obeys exactly the same rules FormFactor does — tag-priority mapping,
52/53-week → calendar-quarter conformance, Q4 = FY−(Q1+Q2+Q3), latest-filed-wins
restatements, and missing-is-flagged-never-filled. The only thing that differs is
the input CIK.

The honest reality this surfaces (and the page shows): US-listed T&M peers tag
GAAP concepts inconsistently. Where a peer simply doesn't file a concept as a
comparable us-gaap tag — Cohu's gross profit, KLA's recent operating income,
Camtek's quarterly data (it files Form 20-F annually as a foreign private
issuer) — the value is left MISSING and flagged, never estimated. The winning
tag actually used for each peer × metric is appended to governance/tag_mapping.csv.

Run AFTER conform.py (which rewrites tag_mapping.csv from scratch).

Usage:
    python src/conform_peers.py
"""

import csv
import json
from pathlib import Path

from conform import build_company_rows

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"
CONFORMED_DIR = REPO_ROOT / "data" / "conformed"
GOVERNANCE_DIR = REPO_ROOT / "governance"
OUT_CSV = CONFORMED_DIR / "fact_peers_quarterly.csv"

# The five GAAP concepts the benchmark compares (gross/operating margin are
# computed ratios of these). We document the winning tag for each in the tag map.
BENCHMARK_FILED = ["revenue", "gross_profit", "operating_income", "rnd_expense"]


def build() -> list[dict]:
    peers = json.loads((RAW_DIR / "peers_manifest.json").read_text())
    as_of = peers["as_of_date"]                # displayed freeze date (2026-07-21)
    rows = []
    for ticker, meta in peers["companies"].items():
        # Reuse the FORM conform path verbatim — same governance, different CIK.
        company_rows = build_company_rows(ticker, {"entity_name": meta["entity_name"]}, as_of)
        rows.extend(company_rows)
        filed = sum(1 for r in company_rows if not r["missing"])
        print(f"  {ticker:5} {len(company_rows)} rows | {filed} filed values | {meta['entity_name']}")
    return rows


def append_tag_mapping(rows: list[dict], as_of: str):
    """Append the ACTUAL winning tag per peer × benchmark metric (idempotent).

    For each peer and each benchmark concept we record the distinct XBRL tag(s)
    that actually fed in-window quarters — or a row noting the concept is not
    comparably tagged, so the governance table on the page tells the true story.
    """
    path = GOVERNANCE_DIR / "tag_mapping.csv"
    existing = list(csv.reader(open(path, encoding="utf-8"))) if path.exists() else []
    have = {(r[0], r[1], r[3]) for r in existing[1:]} if existing else set()  # (ticker, metric, tag)

    # winning tags per (ticker, metric) over in-window filed rows
    seen_tags: dict = {}
    labels: dict = {}
    for r in rows:
        if r["metric_code"] not in BENCHMARK_FILED:
            continue
        labels[r["metric_code"]] = r["metric_label"]
        key = (r["ticker"], r["metric_code"])
        seen_tags.setdefault(key, set())
        if not r["missing"] and r["xbrl_tag"]:
            seen_tags[key].add(r["xbrl_tag"])

    tickers = sorted({r["ticker"] for r in rows})
    new = []
    for ticker in tickers:
        for metric in BENCHMARK_FILED:
            tags = sorted(seen_tags.get((ticker, metric), set()))
            if tags:
                for tag in tags:
                    if (ticker, metric, tag) not in have:
                        new.append([ticker, metric, labels[metric], tag, "peer_filed",
                                    "winning us-gaap tag for this peer/metric", as_of])
            else:
                tag = "(not comparably tagged)"
                if (ticker, metric, tag) not in have:
                    new.append([ticker, metric, labels.get(metric, metric), tag,
                                "peer_missing", "no comparable us-gaap quarterly tag filed", as_of])
    if new:
        with open(path, "a", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(new)
    return len(new)


def main() -> None:
    peers = json.loads((RAW_DIR / "peers_manifest.json").read_text())
    as_of = peers["as_of_date"]
    CONFORMED_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Conforming {len(peers['companies'])} peers (as-of {as_of}) ...")
    rows = build()

    with open(OUT_CSV, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Wrote {OUT_CSV} ({len(rows)} rows)")

    added = append_tag_mapping(rows, as_of)
    print(f"tag_mapping.csv: appended {added} peer tag rows")


if __name__ == "__main__":
    main()
