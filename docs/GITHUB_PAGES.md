# GitHub Pages production build

Production domain: **https://ipointel.brandsap.com**

## Why this exists

GitHub Pages is static hosting. It cannot run FastAPI, PostgreSQL, a
background worker, Caddy, Mailpit, or any server-side secret. The public
research product (landing page, dashboard, IPO detail pages, historical
explorer, track record, methodology) does not actually need a live server at
request time, though - every number on it is a snapshot computed ahead of
time. So the architecture is:

```
Official sources (SEC EDGAR, NSE, market data)
        |
GitHub Actions (.github/workflows/pages.yml) - scheduled + manual
        |
Existing ingestion/scoring pipeline (app/services/pipeline.py, unchanged)
        |
scripts/build_pages.py - freezes real backend output to static JSON
        |
GitHub Pages (dist/) - served at ipointel.brandsap.com
```

The FastAPI/PostgreSQL server (`run_production_local.bat`, Docker, etc.)
is unchanged and keeps working for local/server deployment. Pages is a
second, independent output of the same codebase, not a replacement.

## How the build works

`scripts/build_pages.py` imports `app.main` and calls its existing route
functions **directly, as plain Python functions, in-process** - no HTTP
server, no localhost dependency at build or runtime. This is deliberate:
scoring, DCF, red-flag detection, contradiction detection, similarity
matching and sensitivity analysis are all real, non-trivial logic that
already lives in `app/services/*.py`. The build script never reimplements
any of it; it freezes whatever `ipo_detail()`, `ipo_valuation_detail()`,
`ipo_similar()`, `ipo_changes()`, `track_record()`, `backtest()`, etc.
already compute, straight to JSON.

```
python scripts/build_pages.py --out dist --base-url https://ipointel.brandsap.com \
    --waitlist-endpoint <google-apps-script-url>
```

Reads whichever `DATABASE_URL` is configured (same as every other script in
this repo) - point it at a local SQLite dev DB for a fast offline build, or
at the CI-refreshed SQLite snapshot in production.

### Output (`dist/`)

- `index.html` - landing (public)
- `dashboard/index.html` - the full dashboard, same UI as server mode
- `login/index.html` - present for structural parity; the magic-link flow
  needs a real server, so this page is inert on Pages (honestly, not faked)
- `ipo/<slug>/index.html` - one static, directly-linkable, indexable page
  per published IPO (`<slug>` derived from the same `external_key` the
  pipeline already uses for deduplication - stable across refreshes)
- `data/manifest.json` - build timestamp, 5-year window bounds, counts,
  git commit, model version, `pipeline_status` (worst status among the
  REQUIRED sources: SEC EDGAR, SEC Priced IPOs, NSE, NSE Primary Market
  Reports - never hardcoded), and a full exclusion ledger: every discovered
  active row that was not published is listed under
  `counts.upcoming.excluded` with its id and an explicit reason (stale US
  registration older than 365 days, India issue closed 45+ days without a
  confirmed listing, non-IPO registration, duplicate issuer record,
  superseded by a listed record). `counts.not_ipo`, `counts.withdrawn` and
  `counts.duplicates` are reported separately and never mixed into the
  active counts.
- `data/highlights.json`, `data/upcoming/{india,us}.json` (active statuses
  only: Filed, Upcoming, Open, Closed, Priced), `data/withdrawn.json`,
  `data/history/{india,us}-5y.json` (Listed IPOs only - NSE's monthly report
  also lists preferential allotments, QIPs, rights issues and warrant
  conversions, which are classified `Not IPO` and never published as IPOs),
  `data/ipo/<id>.json`,
  `data/{track-record,source-health,backtest,model-performance}.json`
- Every string in every JSON artifact and generated page passes through
  `app.services.identity.sanitize_tree` - the build fails if any user-visible
  em dash (U+2014) survives, and `tests/test_pages_build.py` asserts it.
- `sitemap.xml`, `robots.txt`, `CNAME`, `404.html`

### Public vs. private data - a real product decision, not an oversight

Every `/api/*` data endpoint in `app/main.py` is gated behind
`require_active_lead` (a beta session). Publishing that same data to GitHub
Pages makes it public **on purpose** - a bundled static JSON file cannot be
made private by client-side JavaScript, so pretending otherwise would be
exactly the kind of fake security this project has explicitly rejected
elsewhere. `app/static/pages-adapter.js` reflects this: in Pages mode, the
"Sign in" nav slot becomes a direct link to the (now public) dashboard
instead of a login prompt. `admin.html` and every `/api/admin/*` capability
are never built into `dist/` at all - those stay server-only, always.

### The frontend didn't get rewritten

`app/static/app.js` and `landing.js` are byte-identical between server mode
and Pages mode. `app/static/pages-adapter.js` intercepts `window.fetch` for
the exact `/api/...` paths those two files already call, and answers them
from the static JSON above instead - `app.js` has no idea which mode it's
running in. `app/static/pages-config.js` is the one file that differs
between deployments (`PAGES_MODE`, `PUBLIC_BASE_URL`,
`PUBLIC_WAITLIST_ENDPOINT`); `build_pages.py` overwrites it in `dist/` with
the real production values.

One deliberate behavior difference: in Pages mode, an unfiltered
`/api/ipos` request (the dashboard's default "All stages" filter) returns
the **upcoming/active** set, not five years of listed history - the
dashboard's own "Listed" status filter is how a visitor reaches the
historical set. This matches the product's primary purpose (upcoming IPO
intelligence) without changing the dashboard's information architecture.

## The rolling 5-year window

`scripts/build_pages.py::history_cutoff()` computes `now - 5 years` from the
actual build timestamp every time - never a hardcoded year. It applies only
to `status == "Listed"` rows, keyed off `listing_date`; an upcoming/filed IPO
is never excluded for having an old `filing_date`. `listing_date` is stored
in two different real formats depending on source (`"30-Nov-2022"` for India,
`"20251022"` for the US SEC feed) - the cutoff always goes through
`app.services.market.parse_date`, the same date parser the rest of the app
already trusts, never a raw string comparison (which silently produces
nonsense across those two formats). Boundary behavior (`test_pages_build.py`):
the exact cutoff date is included, one day before it is excluded, and a row
with an unparseable/missing `listing_date` is excluded from history and
counted as `*_excluded_unparseable_date` in the manifest - never silently
dropped, and never defaulted to 0/today.

### Filling the US side of the window (SEC 424B4 backfill)

Live ingestion (`ingest_sec_priced`) only looks a few days back, so US
"Listed" history would otherwise start on the day production ingestion
began. `scripts/backfill_us_priced.py` walks EDGAR's daily master index
oldest-first over the never-scanned part of the window and stores every
424B4 that is an IPO prospectus through the same `upsert_ipo()` path.

- **Bounded and resumable.** Each run scans `--days` business days or
  `--max-minutes`, commits per day, and records the last completed day on its
  `IngestionRun` row (`metadata_json.through`); the next run resumes there.
  Trigger it from the Actions UI: *Run workflow* with `backfill_us_days`
  (for example `260`, about one year); repeat until the log says
  `completed`. Scheduled runs never backfill.
- **Head-only downloads.** Only the top of each submission is read
  (`sec.filing_head`, stops after the cover page's price table, hard cap
  1.5 MB) and filings whose CIK is already tracked are skipped, so a year of
  filings is a few hundred MB, not tens of GB. Pacing/backoff is the same
  `_get` contract the live ingestion uses.
- **Three-way classification.** `sec.classify_prospectus()` returns `ipo`
  (cover states "this is our initial public offering" / "no public market
  for our shares", or an initial listing application plus the IPO dilution
  statement), `follow_on` (Item 501(b)(4) last-reported-sale-price
  language, "is listed on Nasdaq under the symbol"), or `unknown`. Only
  `ipo` is ever stored. Uplistings from OTC are follow-ons, not IPOs.
- **Repair of existing rows.** `--repair-existing` (always on for the
  dispatch step) re-reads US Listed rows that were stored by the older,
  looser filter: follow-ons become `Not IPO` with provenance, genuine IPOs
  get a missing ticker/final price filled from the cover page, `unknown` or
  cover-not-reached rows keep their status and are flagged so they are not
  re-read. Nothing is deleted or merged.

## Waitlist on GitHub Pages

`PUBLIC_WAITLIST_ENDPOINT` (a GitHub repository **variable**, not a secret -
the deployed `/exec` URL is meant to be publicly callable from the browser,
so there is nothing to protect by hiding it in build logs) points at a
deployed Google Apps Script Web App - see
`integrations/google-apps-script/Code.gs` and its deployment steps in
that file's header comment. No Google service-account credentials or
private key of any kind is ever bundled into `dist/`; Apps Script Web Apps
run under the deploying account's own server-side authorization. If the
variable is unset, `pages-adapter.js` returns an honest "not configured yet"
error instead of a fake success message - see
`test_pages_build.py::test_no_secrets_in_dist` and the build's own
`secret_scan()` step, which fails the build outright if any of
`CLERK_SECRET`, `GOOGLE_APPLICATION_CREDENTIALS`, `AWS_SECRET`,
`RESEND_API_KEY`, a real (non-placeholder) `ADMIN_TOKEN`, a PostgreSQL
`DATABASE_URL`, `POSTGRES_PASSWORD`, or a PEM private-key block ever appears
anywhere under `dist/`.

## Refresh cadence

- Upcoming/current IPO refresh: every ~45 minutes (`schedule: cron`)
- Full daily pass (history, model performance): 06:17 UTC
- Manual: `workflow_dispatch` from the Actions tab, any time
- `data/source-health.json` reports `LIVE` / `DELAYED` / `STALE` / `FAILED` /
  `PARTIAL` per source based on the real last-successful-run timestamp - the
  UI never claims "everything is working" as static marketing copy; it
  reads this file.

A failed refresh (e.g. SEC or NSE briefly unreachable) does not wipe the
site: the workflow persists `data/ipo.db` to a dedicated `data-state` branch
after every successful run (a single force-pushed snapshot commit, not a
growing history) and restores from it at the start of the next run - a
plain git branch, not `actions/cache`, since a fresh cache key every
~45-minute run has no guaranteed hit and is subject to GitHub's 7-day/10GB
cache eviction. That branch only ever contains what this workflow's own
SEC/NSE ingestion wrote (never a local/dev database), so it carries no
waitlist or other private data. See `scripts/refresh_data.py`'s
`continue-on-error: true` step and the restore/persist steps in
`.github/workflows/pages.yml`.

`ingest_nse_history()` (NSE Primary Market Reports) is deliberately not part
of the ~45-minute refresh - like the server worker's own once-a-day cadence
for it, `scripts/refresh_data.py` only runs it when `FULL_REFRESH=true`,
which `pages.yml` sets on the daily 06:17 UTC cron and on manual
`workflow_dispatch` runs.

## Manual steps this repository cannot do for itself

1. **Push this branch to a GitHub remote.** This local repository currently
   has no `git remote` configured at all - confirmed via `git remote -v`
   returning nothing. Nothing in this section (custom domain DNS target,
   the actual deployment, the live Pages URL) can be verified until a target
   repository exists and this branch is pushed to it. See the certification
   report's "Domain" / "GitHub Pages" sections for exactly what is and isn't
   proven yet.
2. **DNS**: once the target repo is known, add a `CNAME` record for
   `ipointel` pointing at `<owner>.github.io` (GitHub Pages requires a CNAME,
   not an A record, for a subdomain). The exact owner is reported in the
   certification once step 1 is resolved.
3. **Repository setting**: Settings -> Pages -> Build and deployment ->
   Source: "GitHub Actions" (one-time, cannot be set via a workflow file).
4. **`PUBLIC_WAITLIST_ENDPOINT` repository variable** (not a secret - see
   "Waitlist on GitHub Pages" above): deploy
   `integrations/google-apps-script/Code.gs` (see its header comment) and
   add the resulting `/exec` URL as a repo variable with that exact name
   (`gh variable set PUBLIC_WAITLIST_ENDPOINT --body "<url>"`).
5. **Clerk** (optional, only if/when used): publishable key only, as a
   repository secret or committed to `pages-config.js` at build time - the
   secret key must never reach `dist/`.
