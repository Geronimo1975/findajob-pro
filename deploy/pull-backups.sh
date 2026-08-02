#!/bin/bash
# =============================================================================
# findajob.pro — pull the VPS database dumps to the laptop.
#
# Repo copy:  deploy/pull-backups.sh
# Runs on:    the LAPTOP, daily, via
#             ~/Library/LaunchAgents/com.geronimo.findajob-backup-pull.plist
#
# `deploy/cron/run-db-backup.sh` makes the nightly dump, but every copy it makes
# lives on the same disk as the database it dumped. Losing the VPS — disk
# failure, a bad `docker compose down -v`, IONOS closing the account — loses
# both at once. That is not a backup, it is a snapshot. This is the other half.
#
# DIRECTION MATTERS: the laptop PULLS. The VPS is never given a credential that
# reaches the laptop, so a compromised VPS cannot touch the off-box copies —
# which is most of the reason to keep them off-box in the first place.
#
# The dumps hold real user emails, so: 0700 on the directory, 0600 on the files,
# and FileVault (verified on) is what protects them at rest.
#
# macOS: bash 3.2, openrsync. No bash 4 syntax.
# =============================================================================

set -euo pipefail

HOST="${FINDAJOB_HOST:-macbook-air}"
REMOTE_DIR="${FINDAJOB_BACKUP_REMOTE:-/root/backups}"
LOCAL_DIR="${FINDAJOB_BACKUP_LOCAL:-$HOME/Backups/findajob-db}"

# Deeper than the VPS keeps (14). Depth is the second reason for an off-box
# copy: the failure you discover on a Tuesday may have started three weeks ago,
# and by then the VPS has already pruned every dump from before it.
KEEP="${FINDAJOB_BACKUP_KEEP:-60}"

# Loudness threshold. A puller that quietly stops working looks exactly like a
# puller with nothing to do, which is how people discover in an emergency that
# the last off-box copy is four months old.
STALE_DAYS="${FINDAJOB_BACKUP_STALE_DAYS:-3}"

STARTED_AT="$(date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"

say() { printf '%s  %s\n' "$(date -u +%H:%M:%SZ)" "$*"; }

printf '=== findajob backup pull | %s ===\n' "$STARTED_AT"

mkdir -p "$LOCAL_DIR"
chmod 700 "$LOCAL_DIR"

# --- pull ---------------------------------------------------------------------
# NOT --delete, deliberately. The VPS prunes at 14; mirroring that would throw
# away exactly the depth this copy exists to provide.
#
# -rlptD rather than -a: no --owner/--group, so the files land owned by the
# local user instead of carrying root:root from the VPS.
#
# BatchMode: this runs unattended under launchd. If the key ever grows a
# passphrase, this must fail loudly rather than hang forever on a prompt nobody
# will ever see.
#
# No --chmod: macOS ships openrsync, which rejects it ("--chmod=F600: invalid
# argument"). The files are chmod'ed after the transfer instead — same result,
# and it also covers copies pulled by an earlier version of this script.
say "pulling from ${HOST}:${REMOTE_DIR}"
if ! rsync -rlptD \
        -e 'ssh -o BatchMode=yes -o ConnectTimeout=20' \
        "$HOST:$REMOTE_DIR/findajob-db-*.sql.gz" "$LOCAL_DIR/" 2>&1; then
    say "PULL FAILED — could not reach ${HOST}, or ssh needed input it could not get."
    say "If the key now has a passphrase, add it to the agent with:"
    say "    ssh-add --apple-use-keychain ~/.ssh/id_ed25519"
    # Fall through to the staleness check rather than exiting: a failed pull is
    # only an emergency if it has ALSO been failing for days, and that is
    # exactly what the check below decides.
fi

# --- verify what we hold ------------------------------------------------------
# Pulling a file proves a transfer happened, not that a restorable backup
# exists. gzip -t is cheap and catches truncation, which is what a half
# transferred dump looks like.
say "verifying local copies"
chmod 600 "$LOCAL_DIR"/findajob-db-*.sql.gz 2>/dev/null || true
bad=0
total=0
for f in "$LOCAL_DIR"/findajob-db-*.sql.gz; do
    [ -e "$f" ] || continue
    total=$((total + 1))
    if ! gzip -t "$f" 2>/dev/null; then
        say "CORRUPT: $(basename "$f")"
        bad=$((bad + 1))
    fi
done
say "$total local dump(s), $bad corrupt"

# --- prune --------------------------------------------------------------------
# Only after verification, and only ever the oldest. Never prune on a failed run.
pruned=0
if [ "$bad" -eq 0 ] && [ "$total" -gt "$KEEP" ]; then
    for old in $(ls -1t "$LOCAL_DIR"/findajob-db-*.sql.gz | tail -n +$((KEEP + 1))); do
        rm -f "$old" && pruned=$((pruned + 1))
    done
    say "pruned $pruned beyond the newest $KEEP"
fi

# --- the check that actually matters ------------------------------------------
newest="$(ls -1t "$LOCAL_DIR"/findajob-db-*.sql.gz 2>/dev/null | head -1 || true)"
verdict=PASS
detail=""

if [ -z "$newest" ]; then
    verdict=FAIL
    detail="no off-box copy exists at all"
else
    # `stat -f %m` is the BSD/macOS spelling; GNU would be `stat -c %Y`.
    age_days=$(( ( $(date +%s) - $(stat -f %m "$newest") ) / 86400 ))
    if [ "$age_days" -gt "$STALE_DAYS" ]; then
        verdict=FAIL
        detail="newest off-box copy is ${age_days}d old (threshold ${STALE_DAYS}d) — the nightly dump or this pull has been broken since then"
    else
        detail="newest $(basename "$newest"), ${age_days}d old"
    fi
fi
[ "$bad" -gt 0 ] && verdict=FAIL

say "$detail"

# Same stable shape as DB-BACKUP and LOGIN-CHECK, so one grep reads all three.
printf 'BACKUP-PULL %s %s held=%s corrupt=%s pruned=%s dir=%s\n' \
    "$STARTED_AT" "$verdict" "$total" "$bad" "$pruned" "$LOCAL_DIR"
printf '\n'

[ "$verdict" = PASS ] || exit 1
