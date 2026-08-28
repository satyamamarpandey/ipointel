#!/usr/bin/env bash
set -euo pipefail
mkdir -p backups
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
docker compose -f docker-compose.production.yml exec -T db pg_dump -U ipo ipo | gzip > "backups/ipo_${STAMP}.sql.gz"
find backups -type f -name 'ipo_*.sql.gz' -mtime +14 -delete
