# PRODUCTION GAPS

Re-measured 2026-09-29 07:19 UTC (`docs/ai_control/measurements/latest.json`).

## Closed today
- [x] India symbol resolution (414 -> 907 of 912) from NSE masters with provenance.
- [x] Forward grader: 0 -> 11 graded; every prediction in one of seven categories; failures recorded.
- [x] Probability wording replaced by scores; `probability_semantics` gated by a numeric release gate.
- [x] India performance 35% -> 96% from official NSE bhavcopies (SME included).
- [x] US final price: 40 missing -> 18 (14 are direct listings with no offer price by definition).
- [x] US pre-IPO financials 0% -> 34% revenue / 47% net income / 45% cash flow on Listed rows.
- [x] Dates canonical `YYYY-MM-DD`; no new duplicate snapshots (tested).
- [x] Offer-price sanity guard for misaligned NSE report rows.
- [x] Walk-forward research with baselines, PR-AUC, Brier, ECE per market and target.

## P0
- [ ] Predictive credibility: no market passes the release gate on honest targets (listing, 12m relative). Needs richer features, not more code: India RHP financials (Q-002), US prospectus table parser (Q-001 B), market regime at listing.

## P1
- [ ] US price series: 323 Listed rows have no Yahoo series (delisted SPACs, micro caps). Needs an alternative source decision (Q-004).
- [ ] Market-regime feature (benchmark return in the 60 days before listing) for both markets.
- [x] Separate US SPAC and operating-company evaluation (Q-009): operating companies pass the gate narrowly; deployment is Q-010.
- [ ] India pre-split ISIN bars for 17 rows whose ISIN changed after listing.
- [ ] 4 US rows with unparseable offer price (MIRA, Telomir combined filings; Hamco below floor; Youxin).
- [ ] Peer valuation sets (valuation coverage 0%).

## P2
- [ ] India RHP/DRHP financial extraction (Q-002).
- [ ] 5 unresolved India identities (InvIT, 4 companies absent from masters).
- [ ] 1,658 legacy redundant snapshot rows (kept per immutability; deduped at read).
- [ ] Rate limiter shared store before running more than one web worker.

## EXTERNAL (human decisions or accounts)
- VPS + DNS A record for `api.ipointel.brandsap.com` (Q-006). Blocks: gated dashboard, email, admin console. Does not block the public site.
- SMTP relay with SPF/DKIM, or `ENABLE_EMAIL=false`. Blocks: email.
- Sentry DSN (optional). Blocks: error reporting.
- Legal review of Terms/Privacy before commercial launch.
- Offsite backup account. Blocks: disaster recovery beyond the VPS.
- Paid US market-data feed (optional, Q-004). Blocks: the 323 US rows without a free series.
