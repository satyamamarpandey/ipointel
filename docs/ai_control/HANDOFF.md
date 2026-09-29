# HANDOFF

Enough for another agent to resume without terminal memory.

## Where things are
- Repo: https://github.com/satyamamarpandey/ipointel, branch `master` is production. Local checkout: `E:\IPO Analysis Dashboard`.
- Public site: https://ipointel.brandsap.com (GitHub Pages). Built by `.github/workflows/pages.yml`: restore `data/ipo.db` from the `data-state` branch, refresh SEC/NSE, build `dist/`, run `tests/test_pages_build.py`, force-push the DB back to `data-state`, deploy. Do not delete or disable that workflow.
- Production database = `git show origin/data-state:data/ipo.db`. Local `data/ipo.db` is gitignored. Copy the snapshot in before measuring or building.
- Backend (FastAPI + Postgres + Caddy) in `docker-compose.production.yml`; not deployed anywhere.
- Control issue: https://github.com/satyamamarpandey/ipointel/issues/5 ("AI Control Plane: IPOIntel Production", labels `ai-control`, `chatgpt-review`). Questions and review checkpoints are posted there and mirrored in QUESTIONS.md.

## How to work
1. `python -m pytest -q` (about 8 minutes; split with `-k` if killed) and `ruff check .` are the gate. Push directly to master.
2. `python scripts/measure_state.py` re-measures everything into `docs/ai_control/measurements/`.
3. Bulk data operations (symbol resolution, performance backfill) run locally against the snapshot, are verified with the measurement script, then the DB is pushed to `data-state` only when no Pages run is in progress (`gh run list --workflow pages.yml --status in_progress`), followed by `gh workflow run pages.yml`.
4. Never modify a `ScoreSnapshot` row (ORM raises). Outcomes go in `PredictionOutcome`.
5. Zero user-visible em dashes; `scripts/build_pages.py` scans for them.

## Key modules
- `app/services/pipeline.py` ingestion + upsert + snapshot events; `identity.py` names/lifecycle; `sec.py`, `nse.py` sources; `market.py` Yahoo prices and return windows; `outcomes.py` grader; `forward_grading.py` grading categories; `walkforward.py` evaluation; `app/scoring.py` heuristic model.
- `scripts/build_pages.py` static build and exclusion ledger; `scripts/backfill_us_priced.py` historical 424B4 backfill.

## Memory of non-obvious rules
See DECISIONS.md and `docs/GITHUB_PAGES.md`.
