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
