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
SEG_CSV_PATH = REPO_ROOT / "data" / "conformed" / "fact_segments_quarterly.csv"
PEERS_CSV_PATH = REPO_ROOT / "data" / "conformed" / "fact_peers_quarterly.csv"
PEERS_MANIFEST = REPO_ROOT / "data" / "raw" / "peers_manifest.json"
DOCS_DIR = REPO_ROOT / "docs"
JSON_OUT = DOCS_DIR / "data" / "form_quarterly.json"
SEG_JSON_OUT = DOCS_DIR / "data" / "form_segments_quarterly.json"
PEERS_JSON_OUT = DOCS_DIR / "data" / "peers_quarterly.json"
INDEX_PAGE = DOCS_DIR / "index.html"       # Phase A — consolidated only
MARGINS_PAGE = DOCS_DIR / "margins.html"   # Phase B — consolidated + segments
PEERS_PAGE = DOCS_DIR / "peers.html"       # Phase C — peer benchmark

# Short display names + roles for the benchmark. FORM is the subject; KLA is a
# margin reference (never ranked as a direct comp).
PEER_DISPLAY = {"FORM": "FormFactor", "TER": "Teradyne", "ONTO": "Onto Innovation",
                "CAMT": "Camtek", "COHU": "Cohu", "INTT": "inTEST", "KLAC": "KLA"}
DIRECT_PEERS = ["TER", "ONTO", "CAMT", "COHU", "INTT"]
REFERENCE = ["KLAC"]
PEER_BENCHMARK_FILED = ["revenue", "gross_profit", "operating_income", "rnd_expense"]

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


def build_segment_payload() -> dict:
    """Package the conformed segment data (Phase B) for margins.html.

    Same shape philosophy as build_payload: one array per (segment, metric) over
    the shared period axis, USD scaled to $millions, missing stays null. We also
    surface `gm_window` — the periods where a reconcilable segment gross margin
    exists — so the page can chart margins only where they tie to GAAP.
    """
    rows = list(csv.DictReader(open(SEG_CSV_PATH, encoding="utf-8")))
    periods = sorted({r["fiscal_period"] for r in rows})
    idx = {p: i for i, p in enumerate(periods)}

    segments: dict = defaultdict(lambda: {"label": "", "metrics": defaultdict(lambda: {
        "values": [None] * len(periods), "qoq_delta": [None] * len(periods),
        "qoq_pct": [None] * len(periods), "yoy_pct": [None] * len(periods),
        "derived": [False] * len(periods), "missing": [True] * len(periods),
    })})
    for r in rows:
        seg = segments[r["segment_code"]]
        seg["label"] = r["segment_label"]
        m = seg["metrics"][r["metric_code"]]
        i = idx[r["fiscal_period"]]
        usd = r["unit"] == "USD"
        scale = USD_TO_MILLIONS if usd else 1.0
        m["values"][i] = opt(r["value"], scale)
        m["qoq_delta"][i] = opt(r["qoq_delta"], scale)
        m["qoq_pct"][i] = opt(r["qoq_pct"])
        m["yoy_pct"][i] = opt(r["yoy_pct"])
        m["derived"][i] = r["derived"] == "True"
        m["missing"][i] = r["missing"] == "True"

    # Periods where BOTH segments have a reconcilable gross margin.
    gm_window = [p for p in periods
                 if segments["probe_cards"]["metrics"]["seg_gross_margin_pct"]["values"][idx[p]] is not None
                 and segments["systems"]["metrics"]["seg_gross_margin_pct"]["values"][idx[p]] is not None]

    # defaultdicts -> plain dicts for JSON
    seg_out = {code: {"label": s["label"], "metrics": {k: dict(v) for k, v in s["metrics"].items()}}
               for code, s in segments.items()}
    first = rows[0]
    return {
        "as_of_date": first["as_of_date"], "ticker": first["ticker"],
        "company": first["company"], "periods": periods,
        "segments": seg_out, "gm_window": gm_window,
        "note": ("Segment revenue reconciles to consolidated revenue; segment gross "
                 "profit is a segment measure and reconciles to consolidated GAAP gross "
                 "profit only via the filed Corporate/unallocated line (FY2024+)."),
    }


def build_peer_payload() -> dict:
    """Package the peer benchmark (Phase 3) for peers.html.

    Combines FormFactor (from the frozen Phase A conformed CSV) with the six
    peers (conform_peers.py) into ONE company-keyed structure. For each company
    we ship the five comparison metrics over the shared period axis — revenue
    ($M) + YoY growth, gross margin %, operating margin %, and R&D % of revenue
    (computed from filed R&D and revenue). Values a peer doesn't comparably tag
    stay null. We also ship the winning XBRL tag per company × concept so the
    page can render the governance tag-mapping table.
    """
    form_rows = list(csv.DictReader(open(CSV_PATH, encoding="utf-8")))
    peer_rows = list(csv.DictReader(open(PEERS_CSV_PATH, encoding="utf-8")))
    peers_manifest = json.loads(PEERS_MANIFEST.read_text())

    by = defaultdict(dict)                        # (ticker, metric) -> {period: row}
    for r in form_rows + peer_rows:
        by[(r["ticker"], r["metric_code"])][r["fiscal_period"]] = r
    periods = sorted({r["fiscal_period"] for r in form_rows})
    idx = {p: i for i, p in enumerate(periods)}

    def series(ticker, metric, field="value", scale=1.0):
        out = [None] * len(periods)
        for p, r in by.get((ticker, metric), {}).items():
            v = r[field]
            if v != "" and r["missing"] != "True":
                out[idx[p]] = round(float(v) * scale, 4 if scale != 1 else 2)
        return out

    def rnd_ratio(ticker):
        rev, rnd = by.get((ticker, "revenue"), {}), by.get((ticker, "rnd_expense"), {})
        out = [None] * len(periods)
        for p in periods:
            rr, dr = rev.get(p), rnd.get(p)
            if rr and dr and rr["missing"] != "True" and dr["missing"] != "True" \
                    and rr["value"] and dr["value"] and float(rr["value"]):
                out[idx[p]] = round(100.0 * float(dr["value"]) / float(rr["value"]), 2)
        return out

    def winning_tag(ticker, metric):
        tags = {r["xbrl_tag"] for r in by.get((ticker, metric), {}).values()
                if r["missing"] != "True" and r["xbrl_tag"]}
        return sorted(tags) if tags else None

    order = ["FORM"] + [t for t in peers_manifest["companies"]]   # FORM, then pull order
    companies = {}
    tagmap = []
    for tk in order:
        role = "subject" if tk == "FORM" else "reference" if tk in REFERENCE else "peer"
        companies[tk] = {
            "label": PEER_DISPLAY.get(tk, tk), "role": role,
            "metrics": {
                "revenue": {"values": series(tk, "revenue", scale=USD_TO_MILLIONS),
                            "yoy_pct": series(tk, "revenue", field="yoy_pct")},
                "gross_margin_pct": {"values": series(tk, "gross_margin_pct")},
                "operating_margin_pct": {"values": series(tk, "operating_margin_pct")},
                "rnd_ratio_pct": {"values": rnd_ratio(tk)},
            },
        }
        for metric in PEER_BENCHMARK_FILED:
            tags = winning_tag(tk, metric)
            tagmap.append({"ticker": tk, "metric": metric,
                           "tags": tags or ["(not comparably tagged)"],
                           "comparable": tags is not None})

    # snapshot = FORM's frozen latest quarter with a revenue value (the anchor/cap)
    snapshot = max(p for p in periods
                   if companies["FORM"]["metrics"]["revenue"]["values"][idx[p]] is not None)
    return {
        "as_of_date": form_rows[0]["as_of_date"],
        "retrieval_date": peers_manifest.get("retrieval_date"),
        "periods": periods, "snapshot": snapshot,
        "companies": companies,
        "direct_peers": DIRECT_PEERS, "reference": REFERENCE, "subject": "FORM",
        "tagmap": tagmap,
        "scope_note": ("US-listed test & measurement peer set. FormFactor's closest "
                       "probe-card competitors (Technoprobe, Micronics Japan, JEM) don't "
                       "file with the SEC, so this is a US-listed peer set, not a pure "
                       "probe-card comp group. KLA is a process-control margin reference, "
                       "not a direct comp."),
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

    # Phase A page: consolidated data only (unchanged behavior).
    inject(INDEX_PAGE, payload_json)
    print(f"Injected consolidated data into {INDEX_PAGE}")

    # Phase B page: consolidated + segments, combined under one data block.
    if SEG_CSV_PATH.exists() and MARGINS_PAGE.exists():
        seg_payload = build_segment_payload()
        SEG_JSON_OUT.write_text(json.dumps(seg_payload, indent=1), encoding="utf-8")
        combined = {"consolidated": payload, "segments": seg_payload}
        inject(MARGINS_PAGE, json.dumps(combined, separators=(",", ":")))
        print(f"Wrote {SEG_JSON_OUT} ({len(seg_payload['gm_window'])} reconcilable GM quarters)")
        print(f"Injected consolidated+segment data into {MARGINS_PAGE}")

    # Phase C page: the peer benchmark (self-contained, FORM + 6 peers).
    if PEERS_CSV_PATH.exists() and PEERS_PAGE.exists():
        peer_payload = build_peer_payload()
        PEERS_JSON_OUT.write_text(json.dumps(peer_payload, indent=1), encoding="utf-8")
        inject(PEERS_PAGE, json.dumps(peer_payload, separators=(",", ":")))
        print(f"Wrote {PEERS_JSON_OUT} (snapshot {peer_payload['snapshot']}, "
              f"{len(peer_payload['companies'])} companies)")
        print(f"Injected peer data into {PEERS_PAGE}")


if __name__ == "__main__":
    main()
