# MODEL STATUS

Updated 2026-09-29 from `measurements/latest.json`.

## Target definitions
- Listing model: listing-day close return vs offer price > 0 (binary). Return base is the offer price; when absent the row is excluded.
- Long-term model: 12-month return vs offer price > 0 (binary). Benchmark-relative variant pending (needs benchmark bars per row).

## Dataset sizes (Listed rows with a score and a realized return)
| Market | Listing n | Positive rate | 12m n | 12m positive |
|---|---|---|---|---|
| India | 305 | 71.1% | 228 | 57.5% |
| US | 720 | 52.4% | 513 | 39.2% |

## Feature coverage on Listed rows
Revenue, growth, EBITDA, PAT, cash flow, debt, valuation, subscription: 0% in both markets. India has issue size (99.8%), offer price (99.8%), sector, board; issue structure 4.6%. US has offer price (96.6%) only.

## Leakage audit
- Historical rows were scored with the same heuristic as live rows, on features present at scoring time. Since no financial features exist for them, no leakage is possible today; the audit must be redone once features are backfilled (availability timestamp per feature, Phase 6).
- `walkforward.evaluate` uses the EARLIEST snapshot per IPO. For rows ingested as Listed that snapshot was written after listing (retrospective, `is_forward=0`); it is a backtest, not forward evidence, and is labelled so.

## Current metrics (heuristic v2.0-evidence-first)
| Market | Model | AUC | Brier | Avg predicted | Actual positive |
|---|---|---|---|---|---|
| India | listing | 0.50 | 0.479 | 18.8% | 71.1% |
| India | 12m | 0.50 | 0.331 | - | 57.5% |
| US | listing | 0.501 | 0.321 | 25.6% | 52.4% |
| US | 12m | 0.502 | 0.247 | - | 39.2% |

Score distribution on Listed rows is a single value per market (India 50.9, US 54.4): the model has no discriminating input. AUC 0.50 is therefore structural, not noise.

## Baselines
Not yet computed. Required before any model claim: base rate, constant prediction, logistic on structural features, current heuristic.

## Forward sample
849 forward snapshots; 0 graded; 8 gradable now. See DATA_STATUS.md.

## Release gate
NOT MET. Model outputs must be presented as scores, confidence-gated, with the limitation stated on the page.
