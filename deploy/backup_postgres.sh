#!/usr/bin/env bash
# On-demand backup (before a risky migration, say). Scheduled backups run as
# the `backup` service in docker-compose.production.yml - see docs/DEPLOYMENT.md.
set -euo pipefail
mkdir -p backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
TARGET="backups/ipo_${STAMP}.sql.gz"

# pg_dump compresses and writes the file itself rather than being piped into
# gzip: a shell pipeline reports the LAST command's exit status, so
# `pg_dump | gzip > f` succeeds even when pg_dump failed, leaving a gzip of
# nothing that looks exactly like a real backup. Writing to .partial first
# means an interrupted dump never lands under the real name either.
if docker compose -f docker-compose.production.yml exec -T db \
        pg_dump -U ipo -Z 9 ipo > "${TARGET}.partial"; then
    mv "${TARGET}.partial" "$TARGET"
    echo "wrote $TARGET ($(wc -c < "$TARGET") bytes)"
    # Pruned only after a confirmed good dump, so a run of failures can never
    # age out the last known-good backup.
    find backups -type f -name 'ipo_*.sql.gz' -mtime +14 -delete
else
    rm -f "${TARGET}.partial"
    echo "backup FAILED - existing backups left untouched" >&2
    exit 1
fi
