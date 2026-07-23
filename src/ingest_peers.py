"""
ingest_peers.py — Phase 3 freeze EXTENSION: pull the US-listed T&M peer bundles.

Cascadia Semiconductors · Phase 3 (peer benchmark)

WHAT & WHY
----------
Phase 3 benchmarks FormFactor against a US-listed test & measurement peer set.
This script pulls the SEC `companyfacts` bundle for each peer and freezes it to
data/raw/, exactly like Phase A's ingest.py — but ADDITIVELY:

  * It never touches FormFactor's frozen bundle or data/raw/manifest.json.
  * CIKs are resolved from the COMMITTED ticker map (data/raw/company_tickers.json),
    never guessed.
  * The peer snapshot is presented as part of the same freeze: its displayed
    as-of date is FormFactor's freeze date (2026-07-21). The ACTUAL retrieval
    date (which may be a day or two later) is recorded separately in
    peers_manifest.json so the provenance is honest.

Peers pulled (six):
  TER  Teradyne · ONTO Onto Innovation · CAMT Camtek · COHU Cohu · INTT inTEST
  KLAC KLA — process-control margin REFERENCE only (not ranked as a direct comp).

SEC etiquette (same as ingest.py): declared User-Agent, ~4 req/s, polite backoff.

Usage:
    python src/ingest_peers.py
"""

import gzip
import json
import time
import urllib.request
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

# Direct T&M peers + KLA as a process-control reference. Order is display order.
PEERS = ["TER", "ONTO", "CAMT", "COHU", "INTT", "KLAC"]

USER_AGENT = "RobbinsAnalytics cascadia-semiconductors ajayrobbins@hotmail.com"
THROTTLE_SECONDS = 0.25
BACKOFF_SCHEDULE = [2, 5, 15, 60]
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"


def polite_get(url: str) -> bytes:
    """GET with base throttle + polite backoff on 429/5xx (mirrors ingest.py)."""
    last = None
    for attempt, backoff in enumerate([0] + BACKOFF_SCHEDULE):
        if backoff:
            print(f"    retrying in {backoff}s (attempt {attempt + 1}) ...")
            time.sleep(backoff)
        time.sleep(THROTTLE_SECONDS)
        req = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"})
        try:
            resp = urllib.request.urlopen(req, timeout=90)
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise                          # 403/404 won't fix itself
        except urllib.error.URLError as e:
            last = e
    raise RuntimeError(f"Failed after retries: {url} ({last})")


def resolve_ciks() -> dict:
    """Resolve peer tickers -> CIK from the COMMITTED SEC ticker map."""
    tm = json.loads((RAW_DIR / "company_tickers.json").read_text(encoding="utf-8"))
    lookup = {row["ticker"].upper(): row["cik_str"] for row in tm.values()}
    ciks = {}
    for t in PEERS:
        if t not in lookup:
            raise SystemExit(f"ERROR: ticker {t} not in committed SEC ticker map.")
        ciks[t] = lookup[t]
    return ciks


def main() -> None:
    # The freeze this extension belongs to (FormFactor's pre-earnings snapshot).
    as_of = json.loads((RAW_DIR / "manifest.json").read_text())["as_of_date"]
    retrieved = date.today().isoformat()       # the ACTUAL pull date, recorded honestly
    print(f"Peer freeze-extension · displayed as-of {as_of} · actually retrieved {retrieved}")

    ciks = resolve_ciks()
    manifest = {
        "as_of_date": as_of,                   # displayed freeze date (tied to FORM)
        "retrieval_date": retrieved,           # when the peer bundles were really pulled
        "note": ("Additive Phase 3 extension of the 2026-07-21 freeze. Peer companyfacts "
                 "pulled after the FORM snapshot; the benchmark never shows a peer quarter "
                 "later than FORM's frozen latest, so the later pull cannot leak newer data."),
        "source": "SEC EDGAR XBRL APIs (https://data.sec.gov/)",
        "user_agent": USER_AGENT,
        "companies": {},
    }

    print(f"Pulling {len(PEERS)} companyfacts bundles ...")
    for ticker, cik in ciks.items():
        url = COMPANYFACTS_URL.format(cik=cik)
        raw = polite_get(url)
        out_name = f"companyfacts_{ticker}_CIK{cik:0>10}.json"
        (RAW_DIR / out_name).write_bytes(raw)
        entity_name = json.loads(raw).get("entityName", "?")
        manifest["companies"][ticker] = {
            "cik": f"{cik:0>10}", "entity_name": entity_name,
            "file": out_name, "url": url, "bytes": len(raw),
        }
        print(f"  {ticker:5} CIK {cik:0>10} · {len(raw):>10,} bytes · {entity_name}")

    (RAW_DIR / "peers_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\nWrote {RAW_DIR / 'peers_manifest.json'} "
          f"(as-of {as_of}, retrieved {retrieved}, {len(PEERS)} peers)")


if __name__ == "__main__":
    main()
