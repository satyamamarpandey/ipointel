# MODEL STATUS

Updated 2026-09-29 07:19 UTC. Source: `measurements/model_latest.json` (`python scripts/evaluate_models.py`).

## Target definitions
- Listing: listing-day close vs offer price > 0.
- 12m absolute: 12-month close vs offer price > 0.
- 12m benchmark-relative (the long-term target used for labels): 12-month return minus the index (NIFTY 50 / S&P 500) over the same window > 0.

## Method
Walk-forward by listing year: the fold for year Y is trained only on rows listed before Y. Ridge logistic regression (Newton) on features available at or before the listing date: structural (log issue size, log offer price, fresh/OFS %, SME flag, SPAC flag) and financial (FeatureObservation rows with `available_at <= listing_date` only). Missing values get indicator columns. Baselines (training base rate, constant 50%, current heuristic v2) are scored on the identical test rows.

## Release gate (per market, per target)
Passed only when all hold: out-of-sample n >= 100, AUC >= 0.58, Brier below the base-rate baseline, and at least 60% of yearly folds with AUC > 0.5. Until then the published number is labelled SCORE (`probability_semantics.*.calibrated = false`).

## Results (out of sample)
| Market | Target | n | Positive | Model AUC | PR-AUC | Brier model / base rate | Gate |
|---|---|---|---|---|---|---|---|
| India | listing | 727 | 77.0% | 0.559 | 0.798 | 0.1807 / 0.1816 | NOT MET (AUC, fold stability) |
| India | 12m | 557 | 60.0% | 0.502 | 0.620 | 0.2476 / 0.2458 | NOT MET |
| India | 12m relative | 557 | 55.5% | 0.536 | 0.599 | 0.2517 / 0.2494 | NOT MET |
| US | listing | 645 | 52.4% | 0.523 | 0.554 | 0.2617 / 0.2518 | NOT MET |
| US | 12m | 408 | 44.4% | 0.807 | 0.825 | 0.1735 / 0.2771 | passes numerically, see note |
| US | 12m relative | 408 | 17.9% | 0.571 | 0.217 | 0.1596 / 0.1481 | NOT MET |

**Note on US 12m absolute:** the signal is the SPAC indicator. SPAC units trade near the $10 trust value while operating-company IPOs on average fall, so "positive 12m return" is mostly "is a SPAC". That is structure, not predictive skill; on the benchmark-relative target the model does not beat the base rate. The long-term label is therefore judged on the relative target and stays SCORE.

## Current heuristic (v2.1-evidence-first)
AUC 0.50 in every market and target. On historical rows it produces one score value per market because the scored features were absent when those rows were ingested. Confidence now includes a freshness penalty for stale active issues.

## Leakage audit
- Financial features enter datasets only through FeatureObservation with `available_at <= listing_date` (tested in `tests/test_model_eval.py`).
- US XBRL values assume the first periodic report's comparative equals the prospectus figure (Q-001). Restatement risk is disclosed and the rule name makes these rows filterable.
- Subscription by category is captured forward only with a day stage, so a day-1 prediction cannot see day-2 data.
- No feature uses post-listing prices.

## Forward sample
862 forward predictions, 11 graded (all US, Sept 2026 listings). Too few for any forward statistic.

## Verdict
Research product: honest, with disclosed limits. Predictive model: NOT production-credible in either market. Outputs are shown as confidence-gated scores with the limitation stated on the page.
