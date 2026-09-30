# PRODUCTION GAPS

Re-measured 2026-09-30 (`docs/ai_control/measurements/latest.json`, `model_latest.json`).

## Closed 2026-09-30
- [x] A-001: post-IPO XBRL comparatives excluded from production datasets; real filing dates restored (3,171 rows).
- [x] US prospectus financial-table parser: 619 issuers parsed (477 revenue, 563 net income, 503 cash flow on Listed rows), point-in-time.
- [x] Market regime features for both markets (2,095 rows).
- [x] Three-tier release gate (A-010) with bootstrap AUC interval, segment stability, distinct forward-IPO count.
- [x] India live listings promoted from NSE masters and NSE past issues (45 IPOs); forward grading 11 -> 395 predictions (35 IPOs).
- [x] India offer prices: official NSE past-issue prices; 11 misaligned monthly-report prices corrected with listing-open confirmation (the 5 former offer_price_suspect rows included).
- [x] Bug fixes: NSE price band parsed as one number; regexes with literal backspace characters in `sec.py`; grader used Yahoo for India; bhavcopy never re-read for late ISINs.

## P0
- [ ] Predictive credibility: only US operating-company listing meets the production tier; deployment is Q-011. India needs DRHP/RHP financials (priority 3) before it can be called production-ready (A-002).

## P1
- [ ] India DRHP/RHP financial extraction (priority 3).
- [ ] US prospectus parser misses: 199 non-SPAC filings without an annual USD table (foreign-currency filers without US$ columns, stub-period layouts).
- [ ] Long-term targets are poorly calibrated in every market (Brier worse than base rate even where AUC > 0.6).
- [ ] US price series: 349 Listed rows have no free series.
- [ ] Forward outcome cross-check against a second source for high-impact US grades (A-004).
- [ ] India pre-split ISIN bars for rows whose ISIN changed after listing; 33 India Listed rows without performance.
- [ ] Peer valuation sets (valuation coverage 0%).

## P2
- [ ] 5 unresolved India identities; 4 unparseable US offer prices.
- [ ] 1,658 legacy redundant snapshot rows (kept per A-007; deduped at read).
- [ ] Rate limiter shared store before more than one web worker.

## EXTERNAL (human decisions or accounts)
- VPS + DNS for `api.ipointel.brandsap.com`: deferred (A-006).
- SMTP relay with SPF/DKIM, or `ENABLE_EMAIL=false`.
- Sentry DSN (optional).
- Legal review of Terms/Privacy before commercial launch.
- Offsite backup account.
- Paid US market-data feed (optional; A-004 says not yet).
