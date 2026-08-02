#!/bin/sh
# =============================================================================
# findajob.pro — nightly PostgreSQL backup.
#
# Repo copy:  deploy/cron/run-db-backup.sh
# VPS path:   /root/findajob/deploy/cron/run-db-backup.sh   (mode 0755)
# Invoked by: /etc/cron.d/findajob-db-backup, under `flock -n`.
#
# Until this existed, /root/backups/ held exactly one findajob dump —
# findajob-prod-FINAL-20260801_1415.sql, taken while wiping the OLD platform.
# Everything written since was recoverable from nowhere. Migrations are the
# sharp edge: a code rollback is three seconds, a schema rollback is not, and
# `migrate` has no undo. This is the precondition for running one at all.
#
# The dump holds real user emails and login history, so it is 0600 root:root
# and gzipped in place. It never leaves the box by itself. (Off-box copies are
# a separate, still-missing job — see the note at the bottom.)
#
# POSIX sh only — /bin/sh on this box is dash.
# =============================================================================

set -u

PROJECT_DIR="${DB_BACKUP_PROJECT_DIR:-/root/findajob}"
SERVICE="${DB_BACKUP_SERVICE:-db}"
DEST_DIR="${DB_BACKUP_DEST:-/root/backups}"
KEEP="${DB_BACKUP_KEEP:-14}"

# A 9MB database dumps to ~75KB in well under a second. 300s is not a budget,
# it is a "something is badly wrong" ceiling — and it sits INSIDE the lock so a
# hung dump can never hold it forever and silently kill every later night.
TIMEOUT_SECONDS="${DB_BACKUP_TIMEOUT:-300}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
STARTED_AT="$(date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
STARTED_EPOCH="$(date -u +%s)"
STAMP="$(date -u +%Y%m%d_%H%M%S)"

if [ -t 1 ]; then INVOKER='manual (tty)'; INV='manual'; else INVOKER='cron'; INV='cron'; fi

TARGET="${DEST_DIR}/findajob-db-${STAMP}.sql.gz"

printf '=== findajob db backup | run %s | %s ===\n' "$RUN_ID" "$STARTED_AT"
printf '    project     : %s\n' "$PROJECT_DIR"
printf '    service     : compose service %s (container findajob-db)\n' "$SERVICE"
printf '    target      : %s\n' "$TARGET"
printf '    retention   : %s newest\n' "$KEEP"
printf '    invoked by  : %s\n' "$INVOKER"

EXIT_CODE=0
BYTES=0
NOTE=''

if ! mkdir -p "$DEST_DIR" 2>/dev/null; then
    NOTE="cannot create ${DEST_DIR}"
    EXIT_CODE=127
elif ! cd "$PROJECT_DIR" 2>/dev/null; then
    NOTE="cannot cd to ${PROJECT_DIR} — is the project still deployed there?"
    EXIT_CODE=127
else
    # Write to a .part file first. A consumer (or the retention pass below)
    # must never be able to see a half-written dump and mistake it for a good
    # one — that is how a backup directory ends up full of files that all look
    # fine and none of which restore.
    #
    # umask BEFORE the redirection: the shell creates the file, so a later
    # chmod would leave a window where the dump is world-readable. It holds
    # real user emails.
    #
    # `exec -T`: cron has no TTY. Credentials come from the CONTAINER's own
    # environment ($POSTGRES_USER/$POSTGRES_DB inside the db container), which
    # is why the inner command is single-quoted — the host shell must not
    # expand them, and this way no password is ever written on a command line
    # visible in `ps`.
    (
        umask 077
        timeout "$TIMEOUT_SECONDS" docker compose exec -T "$SERVICE" \
            sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' 2>/tmp/db-backup-err.$$ \
            | gzip -9 > "${TARGET}.part"
    )
    # In a pipeline `$?` is the LAST command's status (gzip), which is 0 even
    # when pg_dump died and produced nothing. The size and integrity checks
    # below are what actually decide, not the exit code.
    EXIT_CODE=$?
    ERR="$(head -c 500 /tmp/db-backup-err.$$ 2>/dev/null)"
    rm -f /tmp/db-backup-err.$$

    if [ -s "${TARGET}.part" ]; then
        BYTES="$(wc -c < "${TARGET}.part" | tr -d ' ')"
    fi

    if [ "$EXIT_CODE" -eq 124 ]; then
        NOTE="TIMED OUT after ${TIMEOUT_SECONDS}s"
        rm -f "${TARGET}.part"
        EXIT_CODE=124
    elif [ "${BYTES:-0}" -lt 1024 ]; then
        # A valid dump of even an empty schema is several KB gzipped. Anything
        # smaller is a failure that produced a file.
        NOTE="dump too small (${BYTES} bytes) — treating as failure. ${ERR}"
        rm -f "${TARGET}.part"
        EXIT_CODE=1
    elif ! gzip -t "${TARGET}.part" 2>/dev/null; then
        NOTE="gzip integrity check failed"
        rm -f "${TARGET}.part"
        EXIT_CODE=1
    elif ! gzip -cd "${TARGET}.part" 2>/dev/null | head -c 200 | grep -q 'PostgreSQL database dump'; then
        # Proves it is a dump, not 200 bytes of an error message that happened
        # to compress. This is the cheapest thing that distinguishes "a file
        # exists" from "a backup exists".
        NOTE="content does not look like a pg_dump"
        rm -f "${TARGET}.part"
        EXIT_CODE=1
    else
        mv "${TARGET}.part" "$TARGET"
        NOTE="ok"
        EXIT_CODE=0
    fi
fi

# Retention runs ONLY after a verified-good dump. Pruning on a failed night
# would spend a good backup to make room for nothing.
PRUNED=0
if [ "$EXIT_CODE" -eq 0 ]; then
    for old in $(ls -1t "${DEST_DIR}"/findajob-db-*.sql.gz 2>/dev/null | tail -n +$((KEEP + 1))); do
        rm -f "$old" && PRUNED=$((PRUNED + 1))
    done
fi

FINISHED_EPOCH="$(date -u +%s)"
DURATION=$((FINISHED_EPOCH - STARTED_EPOCH))

printf -- '--- result -------------------------------------------------------------\n'
printf '    %s\n' "$NOTE"
printf -- '------------------------------------------------------------------------\n'

if [ "$EXIT_CODE" -eq 0 ]; then VERDICT='PASS'; else VERDICT='FAIL'; fi

# STABLE FORMAT, same shape as the login healthcheck's line so one grep pattern
# reads both. Append freely; never reorder.
printf 'DB-BACKUP %s %s duration=%ss exit=%s bytes=%s pruned=%s run=%s invoked=%s\n' \
    "$STARTED_AT" "$VERDICT" "$DURATION" "$EXIT_CODE" "$BYTES" "$PRUNED" "$RUN_ID" "$INV"
printf '\n'

# STILL MISSING, and it is the honest limit of this job: every copy lives on the
# same disk as the database it backs up. Losing the VPS loses both. An off-box
# destination (or at minimum a periodic `scp` to the laptop) is a separate task.

exit "$EXIT_CODE"
