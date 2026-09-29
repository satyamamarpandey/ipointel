# DECISIONS

| ID | Date | Area | Decision | Rationale | Reversible |
|---|---|---|---|---|---|
| D-001 | 2026-09-07 | DEPLOYMENT | Work directly on master; no PR gate for ordinary Claude work; local tests are the gate. | Solo project, CI latency. | yes |
| D-002 | 2026-09-13 | ARCHITECTURE | Public product is static GitHub Pages built from a SQLite snapshot on the `data-state` branch. Backend serves the gated dashboard only. | Zero-cost, resilient, last-known-good by construction. | yes |
| D-003 | 2026-09-28 | DATA | NSE monthly report rows are IPOs only when `identity.classify_issue_type()` != None; everything else is `Not IPO` and never published. | Report mixes preferential/QIP/rights issues. | no (data) |
| D-004 | 2026-09-28 | DATA | A 424B4 is an IPO only when `sec.classify_prospectus()` says `ipo` from the cover page. | Follow-ons quote a last sale price; IPOs cannot. | no (data) |
| D-005 | 2026-09-29 | DATA | India symbol resolution uses NSE's official equity and SME masters (EQUITY_L.csv, SME_EQUITY_L.csv): ISIN exact match first, then exact canonical-name match; anything else stays UNRESOLVED/AMBIGUOUS/CONFLICTING with a recorded reason. No fuzzy merges. | Tier-1 source, deterministic, provenance-carrying. | yes |
| D-006 | 2026-09-29 | MODEL | Every forward prediction lands in one of seven explicit grading categories; failed grading attempts are recorded on the outcome row. | No silent drops. | yes |
| D-007 | 2026-09-29 | MODEL | Model outputs are labelled SCORE until a walk-forward evaluated model beats the base rate on AUC and Brier per country. | Current AUC 0.50; unsupported probability semantics mislead. | yes |
| D-008 | 2026-09-29 | DATA | India post-listing prices come first from NSE's official daily bhavcopy archive (Tier 1, stored in `price_bars`), Yahoo Finance only as fallback. Yahoo returns no history for NSE SME (Emerge) issues. | 519 of 914 India Listed rows are SME; Yahoo cannot serve them. | yes |
| D-009 | 2026-09-29 | DATA | Yahoo ticker conventions: NSE SME `SYMBOL-SM.NS`, mainboard `SYMBOL.NS`, US SPAC units `XXXXU` -> `XXXX-UN`. One candidate list (`performance.candidate_symbols`) is shared by the history explorer and the forward grader. | Grader and explorer must never disagree on whether a series exists. | yes |
| D-010 | 2026-09-29 | MODEL | Confidence includes a freshness penalty for active issues not refreshed within 7 days (model v2.1). Listed rows are never penalised for age. | Stale live data must not read as high confidence. | yes |
| D-011 | 2026-09-29 | MODEL | Published outputs carry `probability_semantics` per market and target; the label flips from SCORE to PROBABILITY only when `model_eval.release_gate` passes (n>=100, AUC>=0.58, Brier below base rate, >=60% of yearly folds with AUC>0.5). | Numbers, not assertions, decide the label. | yes |
