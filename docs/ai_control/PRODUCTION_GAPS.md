# PRODUCTION GAPS

Re-measured 2026-09-29 from the production snapshot (`docs/ai_control/measurements/latest.json`).

## P0 (credibility of what is published)
- [ ] India symbol resolution: 500 Listed rows without a symbol (Phase 2).
- [ ] Forward grader: 0 of 849 graded; 8 gradable now; categories must be explicit (Phase 9).
- [ ] Probability labels: outputs are uncalibrated; relabel as SCORE until proven (Phase 16).
- [ ] Listed-row feature coverage 0%: the historical model has nothing to learn from (Phases 5-6, 12-14).
- [ ] Performance coverage: India 35%, US 68% (Phases 3-4).

## P1
- [ ] US final price missing on 40 Listed rows (Phase 5).
- [ ] US symbol missing on 31 Listed rows; US Listed rows without a price series (383).
- [ ] Date normalisation to YYYY-MM-DD in storage (Phase 11).
- [ ] Duplicate snapshot prevention (1658 redundant legacy rows; new ones must not appear) (Phase 10).
- [ ] India subscription by category from NSE's official per-issue API, forward-only (Phase 7).
- [ ] 424B4 summary-financials table parser for US (Q-001).
- [ ] Baselines + walk-forward logistic model per country (Phases 13-15).
- [ ] Confidence must reflect feature completeness and freshness (Phase 17).
- [ ] Frontend states for graded/pending/blocked predictions and SCORE labels (Phase 24).

## P2
- [ ] India RHP/DRHP financial extraction (Q-002).
- [ ] NSE bhavcopy cross-check of listing-day prices (Q-004).
- [ ] Similar-IPO matching with missingness penalty (Phase 21).
- [ ] Rate limiter shared store before running more than one web worker.

## EXTERNAL (human decisions or accounts)
- VPS + DNS A record for `api.ipointel.brandsap.com` (Q-006). Blocks: gated dashboard, email, admin console. Does not block the public site.
- SMTP relay with SPF/DKIM, or `ENABLE_EMAIL=false`. Blocks: welcome/alert email.
- Sentry DSN (optional). Blocks: error reporting.
- Legal review of Terms/Privacy before commercial launch.
- Offsite backup account. Blocks: disaster recovery beyond the VPS.
