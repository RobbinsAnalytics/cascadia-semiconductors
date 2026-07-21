# Conformance Rules — Cascadia Semiconductors

These are the rules `src/conform.py` applies when turning raw EDGAR XBRL facts
into the tidy quarterly dataset. They are stated here so every number on the
site can be traced to an explicit, reviewable decision. Data snapshot as-of
**2026-07-21** (see `data/raw/manifest.json`).

## 1. Fiscal-calendar conformance

FormFactor uses a **52/53-week fiscal year** ending the last Saturday of
December; peers added in Phase C differ. To put every company on one grid:

> Each fiscal period is assigned to the **calendar quarter whose quarter-end
> date is nearest to the fiscal period's end date**, requiring the two to be
> within **14 days**. Anything outside that tolerance raises an error rather
> than being silently mislabeled.

FormFactor's fiscal quarter ends fall within ~6 days of calendar quarter ends
(e.g. fiscal Q1 FY2026 ended 2026-03-28 → calendar 2026Q1), so the mapping is
unambiguous in practice. The label `2026Q1` therefore means "the fiscal
quarter that ends nearest to calendar Q1 2026" — fiscal quarters across
companies are **comparable, not identical**, and quarter lengths can be 13 or
14 weeks.

## 2. Period classification

XBRL duration facts are classified by their span:

| Span (days) | Treated as |
|-------------|------------|
| 80–100      | discrete fiscal quarter |
| 350–380     | full fiscal year |
| anything else (e.g. 6/9-month YTD cumulatives) | ignored |

## 3. Tag mapping (no hardcoded single tags)

Companies switch XBRL tags over time. Each metric maps to a **priority list**
of tags (see `tag_mapping.csv`); for any quarter, the highest-priority tag
with a filed fact wins, and the winning tag is recorded on the row. A
fallback tag never overrides a primary tag.

## 4. Derived Q4 (`derived` flag)

Income-statement concepts usually lack a discrete Q4 duration fact — the 10-K
files the full year. Where Q1–Q3 and FY are all present and Q4 is not filed:

> **Q4 = FY − (Q1 + Q2 + Q3)**, flagged `derived = True`.

Filed Q4 facts always beat derivation (FY2018–FY2019 have filed Q4s for most
metrics; FY2020 onward are derived). **Diluted EPS derived this way is
approximate**: per-share amounts are not strictly additive when
weighted-average share counts move between quarters. Derived EPS is rounded
to cents and carries the same `derived` flag plus a page footnote.

## 5. Amended / restated filings (`restated` flag)

When multiple facts exist for the same period (original filing plus
comparatives in later filings, or amendments):

> The **latest filed value wins**. If any earlier filing reported a
> *different* value for the same period, the row is flagged `restated = True`.
> Re-filed identical comparatives are deduplicated silently (not restatements).

## 6. Missing data (`missing` flag)

> Missing data is **flagged, never filled**. No interpolation, no zero-fill,
> no estimates. "Not disclosed" is a first-class value: the grid row exists
> with an empty value and `missing = True`.

## 7. Computed ratios

`gross_margin_pct`, `opex_ratio_pct`, and `operating_margin_pct` are computed
from the conformed GAAP metrics (never from XBRL ratio tags). They inherit
`derived`/`restated` flags from their inputs. Variances for ratio metrics are
expressed in **percentage points** (QoQ/YoY deltas); percent-of-percent
changes are deliberately not computed.

## 8. Variances

QoQ and YoY deltas and percentage changes are precomputed in the conformed
data (`qoq_delta`, `qoq_pct`, `yoy_delta`, `yoy_pct`) — the frontend renders
them but never computes them. Percent change is omitted when the prior-period
value is zero or missing.

## 9. Known GAAP composition note

FormFactor's GAAP operating income includes **gains on divestitures** that sit
outside `OperatingExpenses`: +$72.9M in FY2023 Q4 (FRT Metrology sale) and
+$20.3M in FY2024 Q1 (China business sale), tagged `GainLossOnSaleOfBusiness`.
The validation cross-foot (`validate.py` check 2) accounts for this term, and
the page footnotes it so the operating-margin spikes read correctly.

---
*GAAP per XBRL filings; company headline figures may be non-GAAP. Built from
public SEC filings (EDGAR XBRL APIs). Not investment advice.*
