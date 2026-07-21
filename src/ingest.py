"""
ingest.py — Pull SEC EDGAR XBRL data and freeze it to data/raw/.

Cascadia Semiconductors · Phase A (FormFactor only)

What this script does, in order:
  1. Downloads the SEC's ticker → CIK map (company_tickers.json) so CIKs are
     resolved from an authoritative source, never guessed.
  2. Downloads the full XBRL "companyfacts" bundle for each company in
     COMPANIES (Phase A: FormFactor only).
  3. Writes every response, byte-for-byte as received, into data/raw/,
     alongside a small manifest recording WHEN the pull happened and from
     WHICH URLs. That manifest's `as_of_date` is the freeze stamp that every
     downstream artifact (CSV, SQLite, HTML) carries.

Why freeze? FormFactor reports Q2 FY2026 earnings on 2026-07-29, mid-way
through the interview process this project supports. Freezing the pull means
the dataset cannot silently drift between demos. Re-running this script is a
DELIBERATE refresh — it overwrites the snapshot — so only run it when a
refresh is explicitly intended.

SEC access etiquette (https://www.sec.gov/os/accessing-edgar-data):
  * A descriptive User-Agent identifying the requester is REQUIRED.
  * Stay under 10 requests/second. We throttle to ~4/sec — far below the cap.
  * Back off politely on 429/503 responses.

Usage:
    python src/ingest.py
"""

import json
import time
from datetime import date
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# SEC-required identification: "Sample Company Name AdminContact@sample.com"
USER_AGENT = "RobbinsAnalytics cascadia-semiconductors ajayrobbins@hotmail.com"

# Seconds between requests → ~4 requests/second, well under the SEC's 10/sec cap.
THROTTLE_SECONDS = 0.25

# Retry schedule (seconds) for 429 Too Many Requests / 5xx responses.
BACKOFF_SCHEDULE = [2, 5, 15, 60]

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"

# Phase A pulls FormFactor only. Phases B/C append peers here; the ticker map
# below is the source of truth for each CIK — we cross-check, never guess.
COMPANIES = ["FORM"]

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def polite_get(session: requests.Session, url: str) -> requests.Response:
    """GET a URL with throttling and polite retries on 429/503/5xx.

    Raises for any status that is not eventually 200 — a partial snapshot is
    worse than no snapshot, so we fail loudly rather than continue.
    """
    for attempt, backoff in enumerate([0] + BACKOFF_SCHEDULE):
        if backoff:
            print(f"    retrying in {backoff}s (attempt {attempt + 1}) ...")
            time.sleep(backoff)
        time.sleep(THROTTLE_SECONDS)  # base throttle on every request
        resp = session.get(url, timeout=60)
        if resp.status_code == 200:
            return resp
        if resp.status_code not in (429, 500, 502, 503, 504):
            break  # a 403/404 won't fix itself — stop retrying immediately
    resp.raise_for_status()
    raise RuntimeError(f"Unreachable: {url}")  # pragma: no cover


def resolve_ciks(session: requests.Session) -> dict:
    """Download the SEC ticker map, freeze it, and return {ticker: cik_int}."""
    print(f"[1/2] Ticker → CIK map: {TICKER_MAP_URL}")
    resp = polite_get(session, TICKER_MAP_URL)
    (RAW_DIR / "company_tickers.json").write_bytes(resp.content)

    # File shape: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
    ticker_map = {row["ticker"].upper(): row["cik_str"] for row in resp.json().values()}

    ciks = {}
    for ticker in COMPANIES:
        if ticker not in ticker_map:
            raise SystemExit(f"ERROR: ticker {ticker} not found in SEC ticker map.")
        ciks[ticker] = ticker_map[ticker]
        print(f"      {ticker} → CIK {ticker_map[ticker]:0>10}")
    return ciks


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    as_of = date.today().isoformat()

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    })

    ciks = resolve_ciks(session)

    manifest = {
        "as_of_date": as_of,
        "source": "SEC EDGAR XBRL APIs (https://data.sec.gov/)",
        "user_agent": USER_AGENT,
        "files": {"company_tickers.json": TICKER_MAP_URL},
        "companies": {},
    }

    print(f"[2/2] companyfacts bundles for: {', '.join(COMPANIES)}")
    for ticker, cik in ciks.items():
        url = COMPANYFACTS_URL.format(cik=cik)
        print(f"      {ticker}: {url}")
        resp = polite_get(session, url)
        out_name = f"companyfacts_{ticker}_CIK{cik:0>10}.json"
        (RAW_DIR / out_name).write_bytes(resp.content)

        entity_name = resp.json().get("entityName", "?")
        manifest["companies"][ticker] = {
            "cik": f"{cik:0>10}",
            "entity_name": entity_name,
            "file": out_name,
            "url": url,
            "bytes": len(resp.content),
        }
        print(f"      saved {out_name} ({len(resp.content):,} bytes, entity: {entity_name})")

    (RAW_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\nSnapshot frozen. as_of_date = {as_of}  →  {RAW_DIR / 'manifest.json'}")


if __name__ == "__main__":
    main()
