# DATA STATUS

## 2026-09-30 update
| Metric | India | US |
|---|---|---|
| Listed | 952 | 1,183 |
| Symbols / ISIN | 947 / 952 | 1,157 / n.a. |
| Offer price present | 952 / 952 (11 corrected from NSE past issues with listing-open confirmation) | 1,165 (18 missing) |
| Performance present | 919 | 834 |
| Point-in-time financials (prospectus) | none (DRHP/RHP parser pending) | 619 issuers: revenue 477, net income 563, cash flow 503 Listed rows |
| Market regime at listing | 952 | 1,183 |

Sources added: NSE past issues API (official final price and listing date), SEC prospectus summary tables, index closes (Tier 3) for market regime. XBRL comparatives are display/research only with their real filing date.

---

Updated 2026-09-29 07:19 UTC from `measurements/latest.json` (production snapshot after the Phase 2-11 backfills). Re-run `python scripts/measure_state.py` against the `data-state` snapshot to refresh.

## Coverage (before -> after this push)
| Metric | India | US |
|---|---|---|
| Upcoming discovered / published | 56 / 56 | 178 / 177 (1 duplicate issuer excluded) |
| Listed | 912 (872 in 5y window) | 1184 (1180 in window) |
| Mainboard / SME | 397 / 515 | n/a |
| Symbols resolved | 414 -> **907** (5 unresolved) | 1160 -> 1158 (26 missing; rows reclassified Not IPO) |
| Offer price present | 912 / 912 (1 corrected, 5 flagged suspect) | 1151 -> **1166** (18 missing: 14 direct listings have none by definition, 4 unparsed) |
| Performance present | 320 -> **879 (96.4%)** | 808 -> **835 (70.5%)** |
| Not IPO / Withdrawn | 1185 / 0 | 140 / 1 |

## Feature coverage, Listed rows
| Field | India | US |
|---|---|---|
| identity (ISIN or symbol) | 100% | 97.8% |
| offer price | 100% | 98.5% |
| issue size | 100% | 0% |
| issue structure (fresh/OFS) | 4.6% | 0% |
| revenue | 0% | 0% -> **33.6%** |
| growth | 0% | 0% -> **22.4%** |
| EBITDA | 0% | 0% -> **23.0%** |
| PAT / net income | 0% | 0% -> **46.6%** |
| cash flow | 0% | 0% -> **44.8%** |
| debt | 0% | 0% -> 15.5% |
| valuation inputs (peer set) | 0% | 0% |
| subscription | 0% | n/a |
| performance | 35.0% -> **96.4%** | 67.8% -> **70.5%** |

US financials are post-IPO XBRL comparatives for fiscal years ending before listing (`availability_rule=xbrl_post_ipo_comparative`, Q-001 option A). SPACs (no revenue concept), pre-revenue issuers and non-USD filers stay null by design.

## Feature coverage, upcoming rows
India (56): price band 98.2%, total subscription 91.1%, subscription by category 10.7% (captured live since today, timestamped, forward only). US (178): revenue 36%, PAT 43%, cash flow 41%, debt 20%.

## Forward predictions (862)
GRADED 11, PENDING 0, BLOCKED_IDENTITY 4, BLOCKED_MARKET_DATA 2, NOT_YET_ELIGIBLE 780, INVALID_FORWARD_RECORD 65. Previously 0 graded.
Root cause of zero graded: the old grader scanned the 60 most recently updated Listed rows, which were backfilled historical rows without forward snapshots. Now ledger-driven.

## Price sources
- India: NSE daily bhavcopy (official, Tier 1), 1,253 trading days 2021-09-01 to 2026-09-28 ingested for 911 ISINs, pruned to the bars the return windows read (every return identical, tested). Yahoo has no history for NSE SME issues.
- US: Yahoo Finance (Tier 3). 323 Listed rows have no Yahoo series (mostly delisted SPAC units and micro caps).

## Known data defects (flagged, not guessed)
- 5 India rows `offer_price_suspect`: NSE's Nov-2022 and Sep-2024 monthly reports misaligned rows (Fusion Micro Finance 81 vs real 368; Amiable Logistics 368 vs 81; Bikewo, Avi Ansh, Phoenix Overseas). Listing return suppressed; forward windows measured from listing close.
- 1 India row corrected: Manoj Vaibhav Gems, report price 30, same row's size/shares gives 215, confirmed by the listing-day open 215.
- 49 India rows `no_listing_bar`: 32 listed before 2021-09 (outside the bhavcopy window), the rest changed ISIN after a split so the pre-split bars were not collected (P2).
- 5 India rows unresolved: Kalahridhaan Trendz, Ami Organics, Sahaj Fashions, Varanium Cloud, POWERGRID InvIT.

## Date formats
All stored dates are `YYYY-MM-DD` (3,443 rows normalised; raw kept in `raw`).
