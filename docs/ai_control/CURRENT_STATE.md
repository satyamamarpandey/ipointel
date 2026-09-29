# CURRENT STATE

Updated: 2026-09-29 (UTC). Keep this file short; details live in the sibling files.

- **Current SHA:** see `git log -1` on master (updated at each push; last recorded below).
- **Production:** https://ipointel.brandsap.com, static GitHub Pages built by `.github/workflows/pages.yml` every 3h (full pass daily 06:17 UTC). Pipeline status LIVE.
- **Backend:** deployable (compose + Caddy + Postgres), NOT deployed. `api.ipointel.brandsap.com` has no DNS record.
- **Phase:** 2-11 implemented and being backfilled into the production snapshot; 12-17 research module live (walk-forward, baselines, release gate); 18-22 hardened; 23-26 in progress (full test pass, push, data-state push, live verification).

## Working
- Ingestion: SEC EDGAR S-1/F-1, SEC 424B4 daily index, NSE live issues (now with per-category subscription, timestamped FeatureObservation rows), NSE monthly reports, NSE equity/SME masters (symbols), NSE daily bhavcopy (Tier-1 price bars), SEC XBRL companyfacts (pre-IPO financials with availability dates).
- Lifecycle machine, Not-IPO classification (now also report-artifact rows, follow-on/resale/debt/merger 424B4s), exclusion ledger, last-known-good DB on `data-state`.
- Forward grader with seven explicit categories; every prediction lands in one. Track record JSON carries the ledger.
- Dates canonical `YYYY-MM-DD` in storage (raw kept in `raw`).
- Model outputs labelled SCORE; `probability_semantics` per market/target flips only when `model_eval.release_gate` passes. Confidence includes freshness.

## Broken or weak
- India: 5 Listed rows unresolved (Kalahridhaan Trendz, Ami Organics, Sahaj Fashions, Varanium Cloud, POWERGRID InvIT).
- US: 4 Listed rows with unparseable offer price (MIRA, Telomir: resale prospectus stored; Hamco: $0.25 below plausibility floor; Youxin). 14 direct listings have no offer price by definition.
- Financial features on US Listed rows depend on the first 10-K (Q-001 option A): SPACs, pre-revenue and non-USD filers stay null. India historical financials: none (Q-002).
- Walk-forward research: no market/target passes the release gate on honest targets yet (see MODEL_STATUS.md).
- Yahoo is still the only US price source (Tier 3).

## Active task
Bulk backfills on the production snapshot (bhavcopy 2021-09 to today, US financials, US and India performance), then measure, push code, push `data-state`, dispatch Pages, verify live.

## Next task
RC-001 review checkpoint on issue #5; then Phase 27/28 external questions remain open.

## External blockers
See PRODUCTION_GAPS.md "EXTERNAL". None block the public static product.
