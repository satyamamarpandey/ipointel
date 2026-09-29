# CURRENT STATE

Updated: 2026-09-29 (UTC). Keep this file short; details live in the sibling files.

- **Current SHA:** 8927e54 (master)
- **Production:** https://ipointel.brandsap.com, static GitHub Pages built by `.github/workflows/pages.yml` every 3h (full pass daily 06:17 UTC). Pipeline status LIVE, all four required sources OK.
- **Backend:** deployable (compose + Caddy + Postgres), NOT deployed. `api.ipointel.brandsap.com` has no DNS record.
- **Phase:** 1 complete (re-measurement). Starting Phase 2 (India identity) and Phase 9 (forward grader) in parallel.

## Working
- Ingestion: SEC EDGAR S-1/F-1, SEC 424B4 daily index (priced), NSE live issues, NSE Primary Market Monthly Reports; five-year US 424B4 backfill complete.
- Lifecycle machine, Not-IPO classification, exclusion ledger (discovered == published + excluded), last-known-good DB on `data-state` branch.
- 306 tests passing, ruff clean, CI/CodeQL/Pages green.

## Broken or weak (measured, see measurements/latest.json)
- India: 500 of 914 Listed rows have no symbol; 594 have no performance snapshot.
- US: 383 of 1191 Listed rows have no performance snapshot; 40 lack a final price; 31 lack a symbol.
- Financial feature coverage on Listed rows is 0% in both markets. Every historical listing score is the same number, so AUC is 0.50 by construction.
- Forward track record: 849 predictions, 0 graded. 772 not yet eligible (issue still open/filed/closed), 8 eligible and pending, 4 blocked on identity, 65 invalid (Not IPO / Withdrawn rows scored before classification).
- Displayed "probability" values are a sigmoid of a heuristic score, not calibrated.
- Dates stored in mixed formats (India DD-Mon-YYYY, US YYYYMMDD).

## Active task
Phase 2: resolve India symbols from NSE's official equity/SME masters (ISIN exact match, provenance recorded).

## Next task
Phase 9: forward grader rewrite (explicit categories, no silent drops), then Phase 3/4 market performance backfill.

## External blockers
See PRODUCTION_GAPS.md "EXTERNAL". None block the public static product.
