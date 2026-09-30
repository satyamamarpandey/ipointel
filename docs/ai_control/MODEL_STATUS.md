# MODEL STATUS

Updated 2026-09-30 (UTC). Source: `measurements/model_latest.json` (`python scripts/evaluate_models.py`) on the production snapshot. Decisions A-001..A-010 applied (D-012..D-015).

## Target definitions
- Listing: listing-day close vs offer price > 0.
- 12m absolute: 12-month close vs offer price > 0.
- 12m benchmark-relative (the long-term target used for labels): 12-month return minus NIFTY 50 / S&P 500 over the same window > 0.

## Method
Walk-forward by listing year: the fold for year Y is trained only on rows listed before Y. Ridge logistic regression (Newton). Prediction time is the day before listing. Baselines (training base rate, constant 50%, heuristic v2) are scored on identical test rows. Bootstrap 95% AUC interval (200 resamples).

Features (production dataset, point-in-time only):
- Structural: log issue size, log offer price, fresh/OFS %, SME flag, SPAC flag, log shares offered.
- Financial: revenue, growth, net margin, cash-flow margin, only from `prospectus_filing` observations (424B4/S-1/F-1 summary table or audited statements, `available_at` = EDGAR filing date).
- Market regime: index 60-day return and 20-day volatility from closes strictly before listing; same-market IPO count in the prior 90 days.
Post-IPO XBRL comparatives are excluded (A-001); they feed a separate `research_only` dataset that can never pass the production tier.

## Release gate (per market, per target; D-014)
| Tier | Conditions | Effect |
|---|---|---|
| Screening | n >= 100, AUC >= 0.58, Brier below base rate, >= 60% of folds AUC > 0.5 | research signal worth watching |
| Production | no known leakage, n >= 300, AUC >= 0.60 or bootstrap interval above 0.5, Brier >= 2% below base rate, >= 70% of >= 3 scored folds AUC > 0.5, segment stability (full market AUC at most 0.10 above operating companies) | may drive published output if deployed |
| Probability | production + ECE <= 0.05 + >= 50 distinct IPOs with graded genuine forward outcomes in that market | probability language allowed |

`walkforward.DEPLOYED_RESEARCH_MODEL = False`: every published number is the heuristic, labelled SCORE.

## Results, production dataset (out of sample)
| Market | Target | n | Positive | AUC (95% CI) | Brier model / base | Brier gain | Folds > 0.5 | ECE | Tier |
|---|---|---|---|---|---|---|---|---|---|
| India | listing | 770 | 76.9% | 0.602 (0.545-0.658) | 0.1791 / 0.1821 | 1.6% | 75% | 0.067 | screening (Brier gain short of 2%) |
| India | 12m | 557 | 59.6% | 0.514 (0.467-0.557) | 0.2560 / 0.2470 | none | 33% | 0.109 | none |
| India | 12m relative | 557 | 55.1% | 0.553 (0.498-0.595) | 0.2653 / 0.2501 | none | 100% | 0.139 | none |
| US | listing | 644 | 52.3% | 0.533 (0.492-0.581) | 0.2592 / 0.2519 | none | 100% | 0.121 | none |
| US | 12m | 407 | 44.5% | 0.798 (0.747-0.840) | 0.1839 / 0.2773 | 33.7% | 100% | 0.134 | screening only: SPAC composition (segment gap) |
| US | 12m relative | 407 | 17.9% | 0.584 (0.511-0.654) | 0.1609 / 0.1483 | none | 100% | 0.084 | none |
| US operating cos | listing | 449 | 55.0% | 0.596 (0.541-0.650) | 0.2408 / 0.2491 | 3.3% | 100% (4 folds) | 0.087 | **production** (probability: ECE and forward sample fail) |
| US operating cos | 12m | 326 | 30.7% | 0.633 (0.562-0.695) | 0.2295 / 0.2217 | none | 67% | 0.161 | none |
| US operating cos | 12m relative | 326 | 22.1% | 0.630 (0.554-0.692) | 0.1764 / 0.1730 | none | 100% | 0.099 | none |

Reading:
- India listing moved from 0.559 to 0.602 with market regime features; fold stability now passes. Only the Brier margin (1.6% vs 2%) keeps it out of the production tier.
- US operating-company listing passes the production tier on strictly point-in-time data. Its signal is mostly structural (offer price level) plus market regime; prospectus financials add little for the listing target.
- Long-term targets: AUC is above 0.6 for US operating companies, but probabilities are badly calibrated (Brier worse than the base rate). Discrimination without calibration: not usable.

## Research-only dataset (post-IPO XBRL comparatives, A-001)
Never eligible for production. It is kept for comparison. US operating-company listing is AUC 0.606 there, versus 0.596 on the production dataset. The comparatives do not add real signal once regime features exist.

## Leakage audit
- Production datasets admit only rules `prospectus_filing`, `nse_live_feed`, `market_index_close` with `available_at <= listing date`; unknown rules fail closed (tests: `tests/test_model_gate.py`).
- Evidence that A-001 was right. Among issuers with both sources, the XBRL "comparative" describes a fiscal period later than anything in the prospectus for 36% of revenue rows (127 of 349), 34% of net income rows and 34% of cash-flow rows. Where periods match, the parser agrees with XBRL within 1% for 94% of revenue, 85% of net income and 95% of cash-flow values.
- Market regime uses closes strictly before the listing date (tested with a post-listing crash that must not change the value).
- Subscription by category is forward-only with event stage.

## Forward sample
904 genuine forward predictions. 395 are graded, across 35 distinct IPOs (India 29, US 6); probability language needs 50 per market. India grades come from official NSE bhavcopy bars. 434 predictions are not yet eligible (not listed), 69 are INVALID_FORWARD_RECORD (excluded from denominators, A-008), 4 are BLOCKED_IDENTITY and 2 are BLOCKED_MARKET_DATA.

## Verdict
Research product with disclosed limits. One market-target pair (US operating companies, listing) meets the production tier. It is not deployed pending ChatGPT's decision (Q-011), and it may not use probability language until calibration and the forward sample pass. All other pairs remain research.
