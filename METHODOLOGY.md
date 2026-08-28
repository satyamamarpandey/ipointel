# Evidence-first IPO methodology

The product deliberately separates three questions:

1. **Listing setup** — demand, pricing, issue mechanics and market regime.
2. **Long-term quality** — growth, cash-flow quality, leverage, governance, issue structure and valuation.
3. **Data confidence** — whether the evidence is complete, primary, fresh and conflict-free enough to justify any recommendation.

The current deterministic model is versioned as `v2.0-evidence-first`. It is a transparent baseline, not a claim that fixed weights are permanently optimal. The historical track-record module is designed to support walk-forward calibration when enough point-in-time outcomes exist.

## Overall score weights

| Pillar | Weight |
|---|---:|
| Business evidence | 12% |
| Growth | 12% |
| Financial quality | 16% |
| Valuation | 18% |
| Issue structure | 9% |
| Governance | 9% |
| Demand / mechanics | 10% |
| Market regime | 7% |
| Data confidence | 7% |

Listing and long-term sub-models use different weights. India can use QIB/NII/retail subscription and optional GMP; U.S. scoring uses filing/price/underwriter/market evidence available before trading. GMP is never treated as an official or standalone investment signal.

## Reliability gate

With `STRICT_RELIABILITY=true`, an actionable recommendation is blocked below `MIN_RECOMMENDATION_CONFIDENCE` (70% by default). The output becomes `INSUFFICIENT RELIABLE DATA — NO RECOMMENDATION`.

Source hierarchy:

- Tier 1: SEC EDGAR/XBRL, NSE/BSE/SEBI and official exchange reports.
- Tier 2: issuer/regulatory documents that require document extraction or manual normalization.
- Tier 3: configured secondary/market/sentiment feeds.

A lower-tier source must not silently override conflicting Tier 1 evidence. Each field can store source URL, tier, observed value and conflict status.

## Calibration

The dashboard exposes realized sample size and Brier score for listing-gain probability. It labels the model as insufficiently calibrated when the local realized dataset is too small. Production model changes should use point-in-time, walk-forward tests and should preserve the historical model/version used for each score snapshot.
