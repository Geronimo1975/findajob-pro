#!/bin/sh
# =============================================================================
# findajob.pro — scheduled job ingestion (the low-risk feeds).
#
# Repo copy:  deploy/cron/run-ingest.sh
# VPS path:   /root/findajob/deploy/cron/run-ingest.sh   (mode 0755 — rsync -a,
#             never scp: scp drops the executable bit)
# Invoked by: /etc/cron.d/findajob-ingest, under `flock -n`.
#
# Runs `manage.py ingest_jobs` inside the running api container: RemoteOK
# (JSON API) and We Work Remotely (RSS) — official feeds only, per the risk
# table. The command upserts on (platform, url), so any schedule is safe;
# re-runs refresh rather than duplicate. High-risk platforms have no adapter
# at all and can never be scraped from here.
#
# POSIX sh only — /bin/sh on this box is dash.
# =============================================================================

set -u

PROJECT_DIR="${INGEST_PROJECT_DIR:-/root/findajob}"
SERVICE="${INGEST_SERVICE:-api}"

# Two feed fetches plus a few hundred upserts is seconds. 300s is a
# "something is badly wrong" ceiling, and it sits INSIDE the lock so a hung
# fetch can never hold it forever and kill every later tick. Residual: timeout
# kills the docker CLI on the host, not necessarily the process inside.
TIMEOUT_SECONDS="${INGEST_TIMEOUT:-300}"

RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
STARTED_AT="$(date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
STARTED_EPOCH="$(date -u +%s)"

if [ -t 1 ]; then INVOKER='manual (tty)'; INV='manual'; else INVOKER='cron'; INV='cron'; fi

printf '=== findajob ingest | run %s | %s ===\n' "$RUN_ID" "$STARTED_AT"
printf '    project     : %s\n' "$PROJECT_DIR"
printf '    service     : compose service %s\n' "$SERVICE"
printf '    invoked by  : %s\n' "$INVOKER"

cd "$PROJECT_DIR" || {
    printf 'INGEST %s FAIL duration=0s exit=2 reason=no-project-dir invoked=%s\n' \
        "$STARTED_AT" "$INV"
    exit 2
}

# -T: cron has no TTY. `exec`, not `run`: the running container, the same
# environment real requests use — and no second container left behind.
OUTPUT="$(timeout "$TIMEOUT_SECONDS" docker compose exec -T "$SERVICE" \
    python manage.py ingest_jobs 2>&1)"
EXIT_CODE=$?

[ -n "$OUTPUT" ] && printf '%s\n' "$OUTPUT"

DURATION=$(( $(date -u +%s) - STARTED_EPOCH ))
if [ "$EXIT_CODE" -eq 0 ]; then STATUS=PASS; else STATUS=FAIL; fi
if [ "$EXIT_CODE" -eq 124 ]; then REASON=" reason=timeout"; else REASON=""; fi

# One greppable line per run, same shape as LOGIN-CHECK / DB-BACKUP:
#   grep '^INGEST' /var/log/findajob/ingest.log
printf 'INGEST %s %s duration=%ss exit=%s%s invoked=%s\n' \
    "$STARTED_AT" "$STATUS" "$DURATION" "$EXIT_CODE" "$REASON" "$INV"

exit "$EXIT_CODE"
