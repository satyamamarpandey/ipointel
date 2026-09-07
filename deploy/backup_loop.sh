#!/bin/sh
# Scheduled logical backups, run as a service inside the stack.
#
# Deliberately not a host cron entry: cron on the VPS needs the operator to
# remember to add it, needs docker CLI access, and silently stops existing the
# moment the stack is moved to another machine. A service ships with the
# compose file, so a restored deployment is backing itself up from the first
# `up` with nothing to remember.
#
# Connects to Postgres as an ordinary network client, so this needs no docker
# socket and no privileges beyond the database password.
set -eu

: "${BACKUP_INTERVAL_SECONDS:=86400}"
: "${BACKUP_KEEP_DAYS:=14}"
: "${POSTGRES_HOST:=db}"
: "${POSTGRES_USER:=ipo}"
: "${POSTGRES_DB:=ipo}"
: "${BACKUP_DIR:=/backups}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

mkdir -p "$BACKUP_DIR"
log "started - every ${BACKUP_INTERVAL_SECONDS}s, keeping ${BACKUP_KEEP_DAYS} days"

while true; do
    stamp=$(date -u +%Y%m%dT%H%M%SZ)
    target="$BACKUP_DIR/ipo_${stamp}.sql.gz"

    # Write to .partial and rename only on success. A dump interrupted by a
    # container stop, a full disk or a dropped connection would otherwise
    # leave a truncated .sql.gz sitting there looking exactly like a good
    # backup - which is the kind of thing you discover during a restore.
    #
    # pg_dump compresses and writes the file itself (-Z/-f) rather than being
    # piped into gzip. In a shell pipeline the exit status is the LAST
    # command's, so `pg_dump | gzip > f` reports gzip's success even when
    # pg_dump died - which promoted a gzip of nothing to a "good" backup and
    # then pruned real ones against it. Verified: with a failing pg_dump this
    # form correctly writes nothing.
    if pg_dump -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -Z 9 -f "${target}.partial" "$POSTGRES_DB"; then
        mv "${target}.partial" "$target"
        log "wrote $(basename "$target") ($(wc -c < "$target") bytes)"

        # Optional offsite copy. A backup that lives only on the machine it
        # backs up is not protecting you from that machine dying, which is the
        # main thing backups are for.
        #
        # Deliberately a command hook rather than a built-in S3/B2 client: it
        # commits the project to no vendor, adds no dependency to this image,
        # and works with rclone, aws-cli, b2, rsync or scp equally. The file
        # path is appended as the final argument. See docs/DEPLOYMENT.md.
        if [ -n "${BACKUP_OFFSITE_CMD:-}" ]; then
            if sh -c "$BACKUP_OFFSITE_CMD \"$target\""; then
                log "offsite copy ok: $(basename "$target")"
            else
                # Local backup is still good, so this is not fatal - but it
                # must be loud, because silent offsite failure is how you
                # discover at restore time that nothing was ever uploaded.
                log "WARNING offsite copy FAILED for $(basename "$target") - local copy retained"
            fi
        fi

        # Prune only after a confirmed good backup, so a run of failures can
        # never age out the last known-good dump.
        find "$BACKUP_DIR" -name 'ipo_*.sql.gz' -type f -mtime "+${BACKUP_KEEP_DAYS}" -delete
    else
        rm -f "${target}.partial"
        log "FAILED - keeping existing backups, retrying next cycle"
    fi

    sleep "$BACKUP_INTERVAL_SECONDS"
done
