# IPO Intelligence Terminal — Live Production Edition

A code-first India + U.S. IPO research platform. It has a public early-access page, a live dashboard, a background ingestion worker, strict evidence/confidence gating, historical performance tracking, provenance, score history, and Docker deployment.

## What is implemented

- Early-interest signup at `/` with database persistence, duplicate handling, spam honeypot, rate limiting, referral codes, and optional Resend confirmation email.
- Live dashboard at `/app` with IPO radar, historical performance, source health and model track-record views.
- Automatic browser updates through Server-Sent Events plus a polling fallback.
- U.S. primary ingestion from SEC EDGAR S-1/F-1 feeds + filing text + XBRL company facts.
- India primary ingestion from NSE current/upcoming issue endpoints.
- Official NSE Primary Market monthly report archive parser for historical ingestion.
- Optional secondary Yahoo market-price fallback that is always labeled as a lower-tier source.
- Deterministic 0-100 overall/listing/long-term model; valuation classification; confidence gate; field-level provenance; score snapshots; “what changes the verdict”.
- Evidence-backed red flag engine, cross-source/cross-field contradiction engine, scenario + reverse DCF valuation, quantified recommendation-sensitivity thresholds, historical nearest-neighbour IPO matching, and point-in-time score-change attribution — all deterministic, all derived from structured fields with provenance, none LLM-generated.
- Walk-forward model performance evaluation split by country and by listing/long-term horizon (`/api/model-performance`), gated at a minimum sample size per band — nothing is displayed below that gate.
- Historical performance storage and calibration/Brier-score panel. It explicitly refuses to label the model calibrated when the realized sample is too small.
- FastAPI API docs at `/api/docs`, health checks, tests, GitHub Actions, Docker Compose, Postgres production database and a separate ingestion worker.

## Reliability stance

No system can truthfully promise 100% accuracy about future IPO returns. This project instead enforces a more useful guarantee: if required evidence is missing or contradictory, the dashboard says **INSUFFICIENT RELIABLE DATA — NO RECOMMENDATION**. Set `STRICT_RELIABILITY=true` in production.

## Local run

Windows: double-click `run_local.bat`.

macOS/Linux:

```bash
cp .env.example .env
./run_local.sh
```

Open http://localhost:8010 and http://localhost:8010/app. (Port 8010 is used because
8000 is a common default that's easy to collide with another local process; change
`PORT` in `run_local.bat` and `PUBLIC_BASE_URL` in `.env` together if you want 8000.)

## Production run

1. Copy `.env.example` to `.env` and set a real `SEC_USER_AGENT`, `ADMIN_TOKEN`, public URL and optional Resend key.
2. Set a strong `POSTGRES_PASSWORD` in the environment.
3. Run:

```bash
docker compose up -d --build
```

Put Caddy, Nginx, Cloudflare Tunnel, AWS ALB or another TLS reverse proxy in front of port 8000. The web container serves the product; the worker container performs refreshes.

## Source policy

Tier 1: SEC EDGAR/XBRL, NSE/official exchange reports. Tier 2: regulator/company documents not directly machine readable. Tier 3: secondary market-data fallbacks. A Tier 3 value must never silently override a conflicting Tier 1 value.

## Environment variables

See `.env.example`. Important variables are `DATABASE_URL`, `SEC_USER_AGENT`, `STRICT_RELIABILITY`, `MIN_RECOMMENDATION_CONFIDENCE`, `WORKER_INTERVAL_SECONDS`, `ADMIN_TOKEN`, `ENABLE_EMAIL`, and `RESEND_API_KEY`.

## Notes on India data

NSE public website endpoints can change or apply bot protection. The collector uses a normal browser-style cookie handshake and fails cleanly; it does not attempt to bypass access controls. The source-health dashboard exposes failures rather than substituting invented values.

## Next production extensions

The schema is ready for: DRHP/RHP document extraction, BSE redundancy, registrar/allotment links, anchor-investor persistence, peer-set editor, point-in-time market benchmarks, 6/12/24-month realized returns, score-change email alerts, and a walk-forward retraining job once a sufficiently large labeled dataset exists.

## Historical U.S. backfill

To build a local past-performance set directly from SEC final prospectuses, run a controlled 424B4 backfill. It scans SEC daily master indexes, keeps documents that explicitly describe an initial public offering, extracts the final offer price/ticker when available, and then uses the configured secondary market-price fallback for realized returns:

```bash
python -m app.backfill --days 365 --max-filings 1200
```

Use a real SEC User-Agent and do not raise request frequency aggressively.

## Early-access updates

When `ENABLE_EMAIL=true` and a valid `RESEND_API_KEY` are configured, signups receive confirmation messages and the worker sends material high-confidence score-change alerts. Every email contains an unsubscribe link. With email disabled, signup persistence still works and no outbound message is attempted.

Admin waitlist export is available at `/api/admin/waitlist.csv` with the `X-Admin-Token` header.


### HTTPS production stack

For a public domain, set `DOMAIN` and `POSTGRES_PASSWORD`, point DNS at the host, then run:

```bash
docker compose -f docker-compose.production.yml up -d --build
```

Caddy obtains/renews TLS certificates and reverse-proxies to the FastAPI web service. A PostgreSQL backup helper is included at `deploy/backup_postgres.sh`.

## Browser QA

`tests/` is API-level only (no browser). A real-Chromium smoke suite lives outside
pytest's `testpaths` at `tests_browser/qa_smoke.py` — it needs Playwright's browser
binary, which is too heavy to install on every `pytest` run:

```bash
.venv/Scripts/python.exe -m pip install playwright
.venv/Scripts/python.exe -m playwright install chromium
.venv/Scripts/python.exe tests_browser/qa_smoke.py http://localhost:8010
```

It drives the landing page and dashboard at 4 viewport widths (1440/1024/768/390),
exercises every tab, opens an IPO detail and its lazy-loaded panes, and runs the
waitlist success/invalid/duplicate flows, failing on any console error, failed
`/api/*` request, or >4px horizontal overflow.
