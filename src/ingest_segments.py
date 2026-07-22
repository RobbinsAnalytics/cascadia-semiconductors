"""
ingest_segments.py — Deliberate freeze-EXTENSION: pull FormFactor segment facts.

Cascadia Semiconductors · Phase B (FormFactor segments)

WHY THIS SCRIPT EXISTS (read before running)
--------------------------------------------
Phase A froze the SEC `companyfacts` bundle (data/raw/companyfacts_*.json).
That API returns only NON-dimensional (consolidated) facts, so it does NOT
contain FormFactor's reportable-segment breakdown (Probe Cards vs Systems) —
segment facts are DIMENSIONAL XBRL, carried only in each filing's own XBRL
instance document. Phase B therefore needs one targeted pull of just the
segment concepts, from the filing instances.

This is an EXTENSION of the freeze, not a refresh:
  * It touches ONLY new segment concepts; it never re-pulls or re-stamps any
    Phase A series (those stay exactly as frozen 2026-07-21).
  * It is stamped with the SAME as-of date as Phase A (the manifest's
    as_of_date), because it is pulled inside the same pre-earnings window
    (FORM Q2 FY2026 is filed 2026-07-29; nothing after Q1 FY2026 exists yet).
  * Output is a small, auditable JSON of extracted facts + a manifest listing
    every source filing (accession, URL, filed date) so the pull is fully
    reproducible — re-running reproduces byte-identical output offline-of-drift.

What it pulls: for every 10-Q and 10-K with a report date in the analysis
window (FY2018 Q1 -> Q1 FY2026), it fetches the filing's XBRL instance and
extracts, for each reportable segment, the three segment-level income-statement
concepts FormFactor tags dimensionally:
    RevenueFromContractWithCustomerExcludingAssessedTax   (segment revenue)
    CostOfGoodsAndServicesSold                            (segment COGS)
    GrossProfit                                           (segment gross profit*)

  * NOTE the governance nuance, surfaced honestly downstream: FormFactor's
    SEGMENT gross profit is a segment measure of profit that EXCLUDES certain
    unallocated cost-of-revenue items (stock-based comp, amortization of
    acquisition intangibles), so segment gross profit does NOT sum to
    consolidated GAAP gross profit. Segment REVENUE, by contrast, reconciles
    to consolidated revenue exactly — that exact tie is our correctness gate.

SEC access etiquette (same as ingest.py): declared User-Agent, ~4 req/s,
polite backoff on 429/5xx. See https://www.sec.gov/os/accessing-edgar-data.

Usage:
    python src/ingest_segments.py
"""

import gzip
import json
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw"

CIK = "0001039399"                     # FormFactor, Inc. (zero-padded, authoritative)
TICKER = "FORM"
SUBMISSIONS_URL = f"https://data.sec.gov/submissions/CIK{CIK}.json"
ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"

# Same SEC identification and throttle as ingest.py.
USER_AGENT = "RobbinsAnalytics cascadia-semiconductors ajayrobbins@hotmail.com"
THROTTLE_SECONDS = 0.25                # ~4 req/s, well under the 10/s cap
BACKOFF_SCHEDULE = [2, 5, 15, 60]

# Analysis window: only filings whose period-of-report is FY2018 Q1 onward.
# (Everything earlier is out of the Phase A grid; nothing later than Q1 FY2026
# is filed yet in this pre-earnings freeze.)
WINDOW_START_REPORTDATE = "2018-01-01"

# Dimensional structure we keep. FormFactor tags segment income-statement lines
# on the business-segments axis; since ASU 2023-07 (FY2024+) it ALSO crosses them
# with the consolidation-items axis, and files the unallocated remainder on a
# CorporateNonSegment member. We keep three "buckets": the two reportable
# segments, plus that corporate-unallocated reconciling line (which lets the
# segment gross-profit bridge close exactly to consolidated GAAP gross profit).
#
# We match on member local-name SUBSTRINGS so the extractor is robust to
# member-QName drift across 8 years of filings (e.g. ProbeCardsMember vs
# ProbeCardsSegmentMember).
SEGMENT_AXIS_LOCALNAME = "StatementBusinessSegmentsAxis"
CONSOLIDATION_AXIS_LOCALNAME = "ConsolidationItemsAxis"
OPERATING_SEGMENTS_MEMBER = "OperatingSegmentsMember"      # the per-segment values
CORPORATE_NONSEGMENT_MEMBER = "CorporateNonSegmentMember"  # the unallocated remainder
SEGMENT_MEMBERS = {                    # substring in member local-name -> segment code
    "probecard": "probe_cards",
    "system": "systems",
}
CORPORATE_CODE = "corporate_unallocated"
TARGET_TAGS = {
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "CostOfGoodsAndServicesSold",
    "GrossProfit",
}


def classify_segment(dims: list) -> str | None:
    """Map a context's explicit dimensions to a segment bucket, or None to skip.

    `dims` is a list of (axis_localname, member_localname). We KEEP a fact only
    when its segment membership is unambiguous and NOT cross-tabulated with a
    product / geography / timing axis (those would double-count revenue):

      * {segment axis: Probe/Systems}                      -> that segment
      * {segment axis: Probe/Systems, consolidation: OperatingSegments}
                                                            -> that segment (ASU 2023-07)
      * {consolidation: CorporateNonSegment}               -> corporate-unallocated
      * anything crossed with another axis                 -> None (skip)
    """
    other_axes = [ax for ax, _ in dims
                  if ax not in (SEGMENT_AXIS_LOCALNAME, CONSOLIDATION_AXIS_LOCALNAME)]
    if other_axes:
        return None
    d = dict(dims)
    seg_member = d.get(SEGMENT_AXIS_LOCALNAME)
    consol = d.get(CONSOLIDATION_AXIS_LOCALNAME)
    if seg_member:
        mlow = seg_member.lower()
        code = next((c for sub, c in SEGMENT_MEMBERS.items() if sub in mlow), None)
        if code and consol in (None, OPERATING_SEGMENTS_MEMBER):
            return code
        return None
    if consol == CORPORATE_NONSEGMENT_MEMBER:
        return CORPORATE_CODE
    return None


# ---------------------------------------------------------------------------
# HTTP (throttled, polite retries) — mirrors ingest.py.polite_get
# ---------------------------------------------------------------------------

def polite_get(url: str) -> bytes:
    """GET with base throttle + backoff on 429/5xx; raise on hard failure."""
    last_err = None
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
            last_err = e
            if e.code not in (429, 500, 502, 503, 504):
                raise                  # 403/404 won't fix itself
        except urllib.error.URLError as e:
            last_err = e               # transient network — retry per schedule
    raise RuntimeError(f"Failed after retries: {url} ({last_err})")


def local(tag: str) -> str:
    """Strip the namespace from either notation, returning the bare local name.

    Handles BOTH shapes we meet in an instance:
      * element tags in Clark notation  -> '{http://ns}endDate'          -> 'endDate'
      * dimension/member QName strings  -> 'us-gaap:StatementBusinessSegmentsAxis'
                                                                         -> 'StatementBusinessSegmentsAxis'
    ElementTree gives tags as Clark notation ('}' separator); the `dimension`
    attribute and `explicitMember` text are QName strings (':' separator), so
    we split on both.
    """
    return tag.split("}")[-1].split(":")[-1]


# ---------------------------------------------------------------------------
# Filing selection
# ---------------------------------------------------------------------------

def select_filings() -> list[dict]:
    """Return the 10-Q/10-K filings in-window, newest first, with provenance."""
    subs = json.loads(polite_get(SUBMISSIONS_URL))
    rec = subs["filings"]["recent"]
    out = []
    for i, form in enumerate(rec["form"]):
        if form not in ("10-Q", "10-K"):
            continue
        if rec["reportDate"][i] < WINDOW_START_REPORTDATE:
            continue
        out.append({
            "form": form,
            "report_date": rec["reportDate"][i],
            "filed": rec["filingDate"][i],
            "accession": rec["accessionNumber"][i],
            "primary_doc": rec["primaryDocument"][i],
        })
    out.sort(key=lambda r: r["report_date"], reverse=True)
    return out


def instance_url(filing: dict) -> str:
    """Locate the XBRL instance document inside a filing's folder.

    FormFactor's recent filings are inline XBRL: the instance is
    '<primary>_htm.xml'. Older (pre-iXBRL) filings ship a standalone instance
    '.xml'. To be robust across both, we read the filing's index.json and pick
    the .xml that is the instance — i.e. NOT a linkbase (_cal/_def/_lab/_pre)
    and NOT FilingSummary — preferring an '_htm.xml' when present.
    """
    acc_nodash = filing["accession"].replace("-", "")
    folder = ARCHIVE.format(cik_int=int(CIK), acc_nodash=acc_nodash)
    idx = json.loads(polite_get(folder + "index.json"))
    xmls = [it["name"] for it in idx["directory"]["item"] if it["name"].endswith(".xml")]

    def is_linkbase(n: str) -> bool:
        return n.endswith(("_cal.xml", "_def.xml", "_lab.xml", "_pre.xml")) \
            or n.lower() == "filingsummary.xml"

    candidates = [n for n in xmls if not is_linkbase(n)]
    # Prefer inline-XBRL instance (_htm.xml); else fall back to any candidate.
    htm = [n for n in candidates if n.endswith("_htm.xml")]
    chosen = (htm or candidates)
    if not chosen:
        raise RuntimeError(f"No XBRL instance found in {folder}")
    return folder + chosen[0]


# ---------------------------------------------------------------------------
# Instance parsing — extract segment-axis-ALONE facts
# ---------------------------------------------------------------------------

def parse_contexts(root) -> dict:
    """Map contextRef id -> {start, end, seg, member} for segment/unallocated contexts.

    `seg` is the bucket from classify_segment ('probe_cards' / 'systems' /
    'corporate_unallocated') or None for contexts we don't keep (consolidated
    totals, or facts cross-tabulated with product/geography/timing axes). We
    read dimensions as (local-axis, local-member) so classify_segment can match
    on bare local-names.
    """
    ctx = {}
    for c in root.iter():
        if local(c.tag) != "context":
            continue
        start = end = None
        dims = []
        member_qnames = []
        for e in c.iter():
            le = local(e.tag)
            if le == "startDate":
                start = e.text
            elif le == "endDate":
                end = e.text
            elif le == "explicitMember":
                axis = local(e.get("dimension", ""))
                member = local((e.text or "").strip())
                dims.append((axis, member))
                member_qnames.append((e.text or "").strip())
        if not (start and end):
            continue                   # instant (balance-sheet) contexts — not ours
        seg = classify_segment(dims)
        if seg is not None:
            ctx[c.get("id")] = {"start": start, "end": end, "seg": seg,
                                "member": "|".join(member_qnames)}
    return ctx


def extract_facts(instance_bytes: bytes, filing: dict) -> list[dict]:
    """Pull segment-only revenue / COGS / gross-profit facts from one instance."""
    root = ET.fromstring(instance_bytes)
    ctx = parse_contexts(root)
    facts, seen = [], set()
    for el in root.iter():
        tag = local(el.tag)
        if tag not in TARGET_TAGS:
            continue
        c = ctx.get(el.get("contextRef"))
        if c is None:                  # not a recognized segment / unallocated fact
            continue
        if el.text is None or el.text.strip() == "":
            continue
        # A concept can be tagged more than once per filing for the same segment
        # and period (e.g. revenue appears both segment-alone and under the
        # consolidation-items axis). Keep one row per (tag, segment, period, value).
        key = (tag, c["seg"], c["start"], c["end"], el.text.strip())
        if key in seen:
            continue
        seen.add(key)
        facts.append({
            "tag": tag,
            "segment": c["seg"],
            "member": c["member"],
            "period_start": c["start"],
            "period_end": c["end"],
            "value": int(float(el.text)),
            "accn": filing["accession"],
            "form": filing["form"],
            "filed": filing["filed"],
            "source_doc": filing["primary_doc"],
        })
    return facts


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # Reuse Phase A's freeze stamp: this extension belongs to the SAME snapshot.
    as_of = json.loads((RAW_DIR / "manifest.json").read_text())["as_of_date"]
    print(f"Segment freeze-extension for {TICKER} (CIK {CIK}); as-of {as_of}")

    filings = select_filings()
    print(f"[1/2] {len(filings)} in-window 10-Q/10-K filings "
          f"({filings[-1]['report_date']} .. {filings[0]['report_date']})")

    all_facts, sources, seen_accn = [], [], set()
    print(f"[2/2] fetching + parsing instances ...")
    for f in filings:
        url = instance_url(f)
        facts = extract_facts(polite_get(url), f)
        all_facts.extend(facts)
        if f["accession"] not in seen_accn:
            seen_accn.add(f["accession"])
            sources.append({**f, "instance_url": url, "segment_facts": len(facts)})
        print(f"      {f['form']} {f['report_date']} ({f['filed']}): "
              f"{len(facts):2d} segment facts  <- {url.rsplit('/', 1)[-1]}")

    # Deterministic ordering so the frozen JSON is stable across re-pulls.
    all_facts.sort(key=lambda r: (r["period_end"], r["tag"], r["segment"], r["accn"]))
    payload = {
        "as_of_date": as_of,
        "ticker": TICKER,
        "cik": CIK,
        "source": "SEC EDGAR filing XBRL instances (10-Q/10-K), segment-dimensioned facts",
        "note": ("Segment revenue reconciles to consolidated revenue; segment gross "
                 "profit is a segment measure that excludes unallocated COGS and does "
                 "NOT sum to consolidated GAAP gross profit."),
        "segment_axis": f"us-gaap:{SEGMENT_AXIS_LOCALNAME}",
        "segments": ["probe_cards", "systems", CORPORATE_CODE],
        "tags": sorted(TARGET_TAGS),
        "facts": all_facts,
    }
    out = RAW_DIR / f"segments_{TICKER}_CIK{CIK}.json"
    out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    manifest = {
        "as_of_date": as_of,
        "note": "Provenance for the Phase B segment freeze-extension (see ingest_segments.py).",
        "filings": sources,
    }
    (RAW_DIR / f"segments_manifest_{TICKER}.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"\nWrote {out} ({len(all_facts)} segment facts from {len(sources)} filings)")
    print(f"Wrote {RAW_DIR / f'segments_manifest_{TICKER}.json'}")


if __name__ == "__main__":
    main()
