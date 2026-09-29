# DATA STATUS

Updated 2026-09-29 from `measurements/latest.json`. Re-run `python scripts/measure_state.py` against the `data-state` snapshot to refresh.

## Coverage
| Metric | India | US |
|---|---|---|
| Upcoming discovered / published | 56 / 56 | 181 / 180 (1 duplicate issuer excluded) |
| Listed | 914 (872 in 5y window) | 1191 (1187 in window) |
| Mainboard / SME | 395 / 519 | n/a |
| Symbols resolved / missing | 414 / 500 | 1160 / 31 |
| ISIN present on Listed | 912 | n/a |
| Offer price present / missing | 912 / 2 | 1151 / 40 |
| Performance snapshot present / missing | 320 / 594 | 808 / 383 |
| Not IPO / Withdrawn | 1183 / 0 | 130 / 1 |

## Feature coverage, Listed rows
| Field | India | US |
|---|---|---|
| identity (ISIN or symbol) | 99.8% | 97.4% |
| offer price | 99.8% | 96.6% |
| issue size | 99.8% | 0% |
| issue structure (fresh/OFS) | 4.6% | 0% |
| revenue / growth / EBITDA / PAT / cash flow / debt | 0% | 0% |
| valuation inputs | 0% | 0% |
| subscription (total / by category) | 0% / 0% | n/a |
| performance | 35.0% | 67.8% |

## Feature coverage, upcoming rows
India (56): price band 98.2%, total subscription 89.3%, by-category 0%, issue size 0%. US (181): revenue 35.9%, PAT 43.1%, cash flow 40.9%, debt 19.9%, price band 0%.

## Forward predictions
849 total. GRADED 0, PENDING 8, BLOCKED_IDENTITY 4, BLOCKED_OFFER_PRICE 0, BLOCKED_MARKET_DATA 0, NOT_YET_ELIGIBLE 772, INVALID_FORWARD_RECORD 65.
Root cause of zero graded: `sync_prediction_outcomes` scanned the 60 most recently updated Listed rows, which are the backfilled historical rows without forward snapshots (`no_forward_snapshot: 80`), so the 8 eligible rows were never reached.

## Prospectus coverage
US: 424B4 cover page parsed for every Listed row (price, symbol, IPO classification). No financial statement extraction yet. India: none.

## Source health (last run)
SEC EDGAR ok, SEC Priced IPOs ok, NSE ok, NSE Primary Market Reports ok, SEC 424B4 backfill complete (window 2021-09-28 to 2025-10-23 plus daily since). Yahoo Finance is the only price source (Tier 3).

## Date formats
India listing/open/close dates stored as `DD-Mon-YYYY` or `YYYY-MM-DD HH:MM:SS`; US as `YYYYMMDD`. Parsed correctly by `market.parse_date`, but lexical sorting is wrong. Normalisation is Phase 11.
