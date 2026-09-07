#!/usr/bin/env bash
# Shared deployment lock, sourced by deploy.sh and rollback.sh.
#
# Two deploys racing would interleave a build, a migration and a restart
# against the same stack, so the second must fail fast and say so.
#
# flock is preferred because the kernel releases it when the process dies,
# however it dies - no stale locks after a SIGKILL or a dropped SSH session.
# But flock is not universally present (util-linux; absent from a minimal
# Alpine host, and from Git Bash on Windows). Testing `flock -n 9 || die
# "another deploy is running"` on a box without it produces exactly that
# misleading message on EVERY deploy, so the two cases are distinguished
# here and there is a portable fallback.
#
# The fallback uses mkdir, which is atomic on every POSIX filesystem. Its
# weakness is the mirror of flock's strength: a hard kill leaves the
# directory behind, so a lock older than DEPLOY_LOCK_STALE_SECONDS is
# treated as abandoned and reclaimed.

DEPLOY_LOCK_DIR="${DEPLOY_LOCK_DIR:-/tmp/ipointel-deploy.lock.d}"
DEPLOY_LOCK_FILE="${DEPLOY_LOCK_FILE:-/tmp/ipointel-deploy.lock}"
DEPLOY_LOCK_STALE_SECONDS="${DEPLOY_LOCK_STALE_SECONDS:-3600}"

_lock_die() { echo "$(date -u +%H:%M:%S) lock: $*" >&2; exit 1; }

acquire_deploy_lock() {
    if command -v flock >/dev/null 2>&1; then
        exec 9>"$DEPLOY_LOCK_FILE"
        flock -n 9 || _lock_die "another deploy or rollback is already running (flock on $DEPLOY_LOCK_FILE)"
        return 0
    fi

    # No flock - portable atomic fallback.
    if ! mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null; then
        local age=0
        if [ -d "$DEPLOY_LOCK_DIR" ]; then
            local now mtime
            now=$(date +%s)
            mtime=$(stat -c %Y "$DEPLOY_LOCK_DIR" 2>/dev/null || stat -f %m "$DEPLOY_LOCK_DIR" 2>/dev/null || echo "$now")
            age=$(( now - mtime ))
        fi
        if [ "$age" -gt "$DEPLOY_LOCK_STALE_SECONDS" ]; then
            echo "$(date -u +%H:%M:%S) lock: reclaiming stale lock (${age}s old, held by pid $(cat "$DEPLOY_LOCK_DIR/pid" 2>/dev/null || echo unknown))" >&2
            rm -rf "$DEPLOY_LOCK_DIR"
            mkdir "$DEPLOY_LOCK_DIR" 2>/dev/null || _lock_die "could not reclaim $DEPLOY_LOCK_DIR"
        else
            _lock_die "another deploy or rollback is already running (holder pid $(cat "$DEPLOY_LOCK_DIR/pid" 2>/dev/null || echo unknown), ${age}s old)"
        fi
    fi
    echo $$ > "$DEPLOY_LOCK_DIR/pid"
    # Released on any exit, including the ERR trap path.
    trap 'rm -rf "$DEPLOY_LOCK_DIR"' EXIT
}
