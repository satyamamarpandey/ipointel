# CURRENT STATE

Updated: 2026-09-30 (UTC). Keep this file short; details live in the sibling files.

- **Current SHA:** see `git log -1` on master (last recorded push 5449d69; data-state 869320e).
- **Production:** https://ipointel.brandsap.com, static GitHub Pages built by `.github/workflows/pages.yml` every 3h (full pass daily 06:17 UTC). Pipeline status LIVE.
- **Backend:** deployable and tested, NOT deployed (A-006: deferred until a feature needs server-side state).
- **Phase:** ChatGPT answers A-001..A-010 applied. Priority 1 (US prospectus parser) and priority 2 (market regime) done and backfilled. Priority 4 (forward grading) unblocked for India. RC-002 posted.

## Working
- Ingestion: SEC EDGAR, SEC 424B4 index, NSE live issues (band parsed correctly), NSE past issues (official final price and listing date), NSE monthly reports, NSE equity/SME masters (symbols, ISIN, listing date of live issues), NSE daily bhavcopy (Tier-1 bars, re-read for newly learned ISINs), SEC prospectus summary tables (point-in-time financials), SEC XBRL (display/research only), index closes for market regime.
- Forward grader reads official NSE bars first; 395 graded predictions over 35 IPOs.
- Three-tier release gate; SCORE labels everywhere; research table on the model page marked RESEARCH.

## Broken or weak
- India historical financials: none (DRHP/RHP parser is priority 3, required before India is production-ready, A-002).
- US: 199 non-SPAC prospectuses yielded no annual USD table (foreign-currency filers without a US$ column, unusual layouts). 349 US Listed rows lack a free price series.
- India listing model is just short of the production tier (Brier gain 1.6% vs 2%). US operating-company listing passes production but is not deployed (Q-011).
- Long-term targets: discrimination without calibration in every market.

## Active task
Priority 3: India DRHP/RHP financial extraction.

## Next task
Priority 5: re-run evaluation once India financials exist; forward grading continues daily.

## External blockers
See PRODUCTION_GAPS.md "EXTERNAL". None block the public static product.
