#!/usr/bin/env bash
# Roll the application code back to a previous revision. Run on the VPS:
#
#   ./scripts/rollback.sh <sha|tag>
#   ./scripts/rollback.sh --list      # recent revisions to choose from
#
# CODE ONLY. This never touches the database: the Postgres volume is left
# exactly as it is, and no Alembic downgrade is run. See
# docs/DEPLOYMENT.md "Migration rollback policy" for why forward-fix is the
# default for schema problems.
set -euo pipefail

COMPOSE_FILE=docker-compose.production.yml
COMPOSE="docker compose -f $COMPOSE_FILE"

log() { echo "$(date -u +%H:%M:%S) rollback: $*"; }
die() { echo "$(date -u +%H:%M:%S) rollback: FAILED - $*" >&2; exit 1; }

if [ "${1:-}" = "--list" ]; then
    git log --oneline -15
    exit 0
fi

TARGET="${1:-}"
[ -n "$TARGET" ] || die "usage: ./scripts/rollback.sh <sha|tag>   (--list to see options)"
[ -f "$COMPOSE_FILE" ] || die "run this from the repository root"

# Shares the deploy lock: a rollback racing a deploy is the same hazard.
. "$(dirname "$0")/_lock.sh"
acquire_deploy_lock

git rev-parse --verify --quiet "$TARGET^{commit}" >/dev/null || die "unknown revision: $TARGET"
TARGET_SHA=$(git rev-parse "$TARGET")

log "from $(git rev-parse HEAD)"
log "to   $TARGET_SHA ($(git log -1 --format=%s "$TARGET_SHA" | cut -c1-60))"

# The danger case: rolling code back past a migration that has already run.
# The old code then meets a newer schema. Additive migrations are usually
# fine; a destructive one is not, and that judgement cannot be automated.
CURRENT_MIGRATIONS=$(git ls-tree -r --name-only HEAD alembic/versions | wc -l)
TARGET_MIGRATIONS=$(git ls-tree -r --name-only "$TARGET_SHA" alembic/versions | wc -l)
if [ "$TARGET_MIGRATIONS" -lt "$CURRENT_MIGRATIONS" ]; then
    echo
    echo "WARNING: the current revision has $((CURRENT_MIGRATIONS - TARGET_MIGRATIONS)) migration(s) that $TARGET does not."
    echo "The database will KEEP the newer schema - this script does not downgrade."
    echo "That is safe for additive migrations and unsafe for destructive ones."
    echo "Check what changed before continuing:"
    echo "  git diff --stat $TARGET_SHA..HEAD -- alembic/versions"
    echo
    printf "Continue? [y/N] "
    read -r reply
    [ "$reply" = "y" ] || [ "$reply" = "Y" ] || die "aborted by operator"
fi

git checkout --quiet --detach "$TARGET_SHA"
log "rebuilding and restarting (database untouched)..."
$COMPOSE up -d --build web worker caddy backup

log "waiting for health..."
for i in $(seq 1 30); do
    if $COMPOSE exec -T web python -c "
import sys, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3); sys.exit(0)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        log "healthy after ${i}s - now running $TARGET_SHA"
        $COMPOSE ps
        exit 0
    fi
    sleep 1
done

die "health check never passed - the rollback target may itself be broken; inspect: $COMPOSE logs web"
