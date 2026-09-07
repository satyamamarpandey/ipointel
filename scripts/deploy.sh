#!/usr/bin/env bash
# Repeatable production deploy. Run on the VPS from the repository root:
#
#   ./scripts/deploy.sh              # deploy origin/master
#   ./scripts/deploy.sh <sha|tag>    # deploy a specific revision
#
# Pulls the requested revision, builds, migrates, restarts, and verifies
# health - and refuses to leave a half-deployed stack behind on failure.
set -euo pipefail

COMPOSE_FILE=docker-compose.production.yml
COMPOSE="docker compose -f $COMPOSE_FILE"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/health}"
TARGET="${1:-origin/master}"

log() { echo "$(date -u +%H:%M:%S) deploy: $*"; }
die() { echo "$(date -u +%H:%M:%S) deploy: FAILED - $*" >&2; exit 1; }

# Refuses to run if another deploy/rollback holds the lock. See scripts/_lock.sh.
. "$(dirname "$0")/_lock.sh"
acquire_deploy_lock

[ -f "$COMPOSE_FILE" ] || die "run this from the repository root"
[ -f .env ] || die ".env missing - see docs/LAUNCH.md (compose reads .env, not .env.production)"

# A deploy must be reproducible from git, so anything uncommitted here means
# the running stack would not match any revision on GitHub.
if [ -n "$(git status --porcelain)" ]; then
    die "working tree is dirty - commit or stash first (git is the source of truth)"
fi

PREVIOUS_SHA=$(git rev-parse HEAD)
log "current revision $PREVIOUS_SHA"

log "fetching..."
git fetch --quiet origin
git checkout --quiet --detach "$TARGET"
DEPLOY_SHA=$(git rev-parse HEAD)
log "deploying $DEPLOY_SHA ($(git log -1 --format=%s | cut -c1-60))"

# Roll back to the previous revision if anything below fails, so a failed
# deploy leaves the box on the last revision that actually worked rather than
# on a broken half-built one.
rollback_on_failure() {
    log "deploy failed - returning to $PREVIOUS_SHA"
    git checkout --quiet --detach "$PREVIOUS_SHA" || true
    $COMPOSE up -d --build web worker caddy backup || true
    die "rolled back to $PREVIOUS_SHA (database left untouched)"
}
trap rollback_on_failure ERR

log "building images..."
$COMPOSE build

# Backup before schema changes. A migration is the one step that is not
# trivially reversible, so this is the last safe point to capture state.
log "taking a pre-migration backup..."
$COMPOSE up -d db
$COMPOSE exec -T db sh -c 'until pg_isready -U ipo -q; do sleep 1; done'
mkdir -p backups
PRE="backups/pre-deploy_$(date -u +%Y%m%dT%H%M%SZ).sql.gz"
if $COMPOSE exec -T db pg_dump -U ipo -Z 9 ipo > "${PRE}.partial"; then
    mv "${PRE}.partial" "$PRE"
    log "pre-migration backup: $PRE ($(wc -c < "$PRE") bytes)"
else
    rm -f "${PRE}.partial"
    die "pre-migration backup failed - refusing to migrate without one"
fi

log "running migrations..."
$COMPOSE run --rm migrate
log "alembic revision now: $($COMPOSE run --rm --entrypoint '' migrate python -m alembic current 2>/dev/null | tail -1)"

log "restarting services..."
$COMPOSE up -d --build

log "waiting for health..."
for i in $(seq 1 30); do
    if $COMPOSE exec -T web python -c "
import sys, urllib.request
try:
    urllib.request.urlopen('$HEALTH_URL', timeout=3); sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        trap - ERR
        log "healthy after ${i}s"
        log "deployed $DEPLOY_SHA"
        $COMPOSE ps
        exit 0
    fi
    sleep 1
done

die "health check never passed after 30s - inspect: $COMPOSE logs web"
