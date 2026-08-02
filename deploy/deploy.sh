#!/usr/bin/env bash
# =============================================================================
# findajob.pro — deploy the static frontend to the VPS.
#
#   deploy/deploy.sh web [--dry-run] [--yes] [--skip-ci-check]
#
# Only the `web` target exists today. `api`, `nginx` and `traefik` are
# deliberately still manual (see deploy/README.md) — they will be added here
# once this path has been exercised a few times, not before.
#
# WHY THIS EXISTS AT ALL, given `rsync -az --delete web/ …` is one line:
#
# On 2026-08-02 the graphify knowledge-graph output was found being served
# publicly at https://findajob.pro/graphify-out/graph.json — 119KB of the
# internal codebase map, 200 OK. Nobody put it there on purpose. The chain:
# the post-commit hook regenerates web/graphify-out/, .gitignore hides it from
# `git status`, and `rsync --delete web/` DOES NOT READ .gitignore. The working
# tree is not the deliverable and never was.
#
# So the payload here comes from `git archive`, not from the working tree. That
# is the whole point of the script: it is structurally unable to publish a file
# that is not committed. `--exclude` would not have been a fix — it also shields
# the path from `--delete`, so anything already on the box stays there forever.
#
# Runs on the LAPTOP (macOS: bash 3.2, openrsync). No bash 4 syntax.
# =============================================================================

set -euo pipefail

HOST="${FINDAJOB_HOST:-macbook-air}"
ROOT="${FINDAJOB_ROOT:-/root/findajob}"
KEEP_SNAPSHOTS=5

TARGET=""
DRY_RUN=0
ASSUME_YES=0
SKIP_CI=0

die()  { printf 'deploy.sh: %s\n' "$*" >&2; exit 1; }
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }

usage() {
    sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        web)              TARGET=web; shift ;;
        api|nginx|traefik) die "target '$1' is not implemented yet — see deploy/README.md and do it by hand" ;;
        --dry-run)        DRY_RUN=1; shift ;;
        --yes|-y)         ASSUME_YES=1; shift ;;
        --skip-ci-check)  SKIP_CI=1; shift ;;
        -h|--help)        usage 0 ;;
        *)                printf 'deploy.sh: unknown argument %s\n\n' "$1" >&2; usage 2 ;;
    esac
done
[ -n "$TARGET" ] || { printf 'deploy.sh: no target given\n\n' >&2; usage 2; }

cd "$(git rev-parse --show-toplevel)" || die "not inside the repo"

SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short=12 HEAD)"
TS="$(date -u +%Y%m%d_%H%M%S)"

# --- 1. preflight -------------------------------------------------------------
# Everything here is a refusal to deploy something you cannot later identify.

say "preflight"

# The payload comes from HEAD, so an uncommitted edit would silently NOT ship.
# Failing loudly beats deploying yesterday's file while looking at today's.
git diff --quiet --ignore-submodules -- web \
    || die "web/ has uncommitted changes — the payload comes from HEAD, so they would not ship. Commit or stash them."
git diff --cached --quiet --ignore-submodules -- web \
    || die "web/ has staged-but-uncommitted changes — same problem. Commit them."

# /root/findajob is NOT a git repo (checked: deploy/README.md §8 documents a
# `git -C /root/findajob checkout` that cannot possibly run). So the pushed
# remote is the only durable record of what is on the box. Deploying an
# unpushed commit produces a box whose state exists nowhere else.
git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1 || die "no upstream branch configured"
[ "$(git rev-list --count '@{u}..HEAD')" = 0 ] \
    || die "HEAD is not pushed. /root/findajob is not a git repo, so the remote is the only record of what is deployed. Push first."

# CI gate. An EMPTY check-runs array is treated as failure, not as "nothing to
# see": push-then-immediately-deploy is the most common case, and there the runs
# have not registered yet — exactly when you least want a silent pass.
if [ "$SKIP_CI" -eq 0 ]; then
    ok_count="$(gh api "repos/{owner}/{repo}/commits/$SHA/check-runs" \
        --jq '[.check_runs[] | select(.name == "backend" and .conclusion == "success")] | length' 2>/dev/null || echo 0)"
    [ "${ok_count:-0}" -ge 1 ] \
        || die "CI is not green for $SHORT (the 'backend' job has no successful run yet). Wait for it, or pass --skip-ci-check during an incident."
    note "CI: backend green for $SHORT"
else
    note "CI gate SKIPPED (--skip-ci-check)"
fi

# --- 2. payload from git, never from the working tree -------------------------

say "payload"

PAYLOAD="$(mktemp -d)"
trap 'rm -rf "$PAYLOAD"' EXIT
git archive --format=tar "$SHA" web | tar -x -C "$PAYLOAD"
[ -d "$PAYLOAD/web" ] || die "git archive produced no web/ — is the tree intact?"
note "$(find "$PAYLOAD/web" -type f | wc -l | tr -d ' ') tracked files from $SHORT"

# The pin for the incident that motivated this script. If graphify output (or
# anything else gitignored) ever gets committed, this is the last line of
# defence before it is public again. CI has the same assertion; belt and braces,
# because CI can be skipped and this cannot.
if [ -d "$PAYLOAD/web/graphify-out" ]; then
    die "web/graphify-out/ is in the payload — that is the internal codebase map and it must never be published. Remove it from git."
fi

if [ "$DRY_RUN" -eq 1 ]; then
    say "dry run — what WOULD change on $HOST:$ROOT/web/"
    rsync -rlptDn --delete --itemize-changes "$PAYLOAD/web/" "$HOST:$ROOT/web/" \
        || die "rsync dry run failed"
    note "nothing was written. Re-run without --dry-run to apply."
    exit 0
fi

if [ "$ASSUME_YES" -eq 0 ]; then
    printf 'Deploy web/ at %s to %s:%s ? [y/N] ' "$SHORT" "$HOST" "$ROOT"
    read -r reply
    case "$reply" in y|Y|yes) ;; *) die "aborted" ;; esac
fi

# --- 3. snapshot BEFORE the transfer -----------------------------------------
# Order is load-bearing and got this wrong in the first draft: ./web is a
# DIRECTORY bind mount, so the rsync in step 4 IS the application — there is no
# separate "apply" step to snapshot before. Taking the snapshot afterwards would
# photograph the already-broken state.
#
# `cp -al` hardlinks, so a snapshot costs inodes and no data. It stays correct
# because rsync writes a temp file and renames it, which breaks the link rather
# than editing the snapshot in place — do NOT add --inplace, it would corrupt
# every snapshot at once.

say "snapshot"

ssh "$HOST" "set -e
    mkdir -p '$ROOT/.deploy-state'
    rm -rf '$ROOT/.deploy-state/web-$TS'
    cp -al '$ROOT/web' '$ROOT/.deploy-state/web-$TS'
    sha256sum '$ROOT/web/legal.js' | awk '{print \$1}' > '$ROOT/.deploy-state/legal-$TS.sha256' || true
    ls -1dt '$ROOT'/.deploy-state/web-* 2>/dev/null | tail -n +$((KEEP_SNAPSHOTS + 1)) | xargs -r rm -rf"
note "web-$TS (keeping $KEEP_SNAPSHOTS)"

ROLLBACK="ssh $HOST \"rm -rf $ROOT/web.failed && mv $ROOT/web $ROOT/web.failed && cp -al $ROOT/.deploy-state/web-$TS $ROOT/web\""

# --- 4. transfer --------------------------------------------------------------
# -rlptD, not -a: -a would additionally carry owner and group, which is how
# every file in the webroot ended up owned by 501:staff — the laptop's uid,
# meaningless on the VPS. Dropping -o -g lets them land as root:root.
#
# --delay-updates: rsync 3.x deletes DURING the transfer by default, so a
# network drop halfway through leaves the live site missing files. This stages
# everything and swaps at the end.
#
# NOT a directory `mv`: Docker resolves a bind mount at container CREATE, so
# replacing the directory would silently detach nginx from the new one.

say "transfer"
rsync -rlptD --delete --delay-updates "$PAYLOAD/web/" "$HOST:$ROOT/web/" \
    || die "rsync failed — the site may be mid-update. Roll back with:
  $ROLLBACK"

# --- 5. legal.js gate ---------------------------------------------------------
# web/legal.js is both what the browser renders AND what `manage.py seed_legal`
# hashes into LegalDocument. Consent is resolved server-side at verify time and
# fails CLOSED with 503 when unseeded — so shipping a changed legal.js without
# re-seeding means nobody can complete a first login.

# The comparison is done HERE, not over ssh: `cmp -s <(…)` is process
# substitution, which is bash-only, and the remote command runs in root's login
# shell — dash on a stock Debian. It would fail with a syntax error and, worse,
# the `if !` would read that failure as "legal.js changed" and re-seed on every
# single deploy.
LEGAL_NOW="$(ssh "$HOST" "sha256sum '$ROOT/web/legal.js' 2>/dev/null | awk '{print \$1}'")"
LEGAL_WAS="$(ssh "$HOST" "cat '$ROOT/.deploy-state/legal-$TS.sha256' 2>/dev/null || true")"
if [ -z "$LEGAL_NOW" ] || [ "$LEGAL_NOW" != "$LEGAL_WAS" ]; then
    say "legal.js changed — re-seeding"
    ssh "$HOST" "cd '$ROOT' && docker compose exec -T api python manage.py seed_legal" \
        || die "seed_legal FAILED. Consent fails closed with 503, so first-login is broken until this succeeds. Fix it now, or roll back:
  $ROLLBACK"
else
    note "legal.js unchanged — no re-seed needed"
fi

# --- 6. verify ----------------------------------------------------------------
# Read the result back rather than trusting an exit code. Every trap on this box
# is of the form "the tool reports success and changes nothing".

say "verify"

fail=0
check() {  # check <label> <url> <expected-code>
    got="$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$2" || echo 000)"
    if [ "$got" = "$3" ]; then
        printf '    \033[32mok\033[0m   %-34s %s\n' "$1" "$got"
    else
        printf '    \033[31mFAIL\033[0m %-34s got %s, want %s\n' "$1" "$got" "$3"
        fail=$((fail + 1))
    fi
}

check "landing"              "https://findajob.pro/"                        200
check "dashboard"            "https://findajob.pro/app.html"                200
check "auth"                 "https://findajob.pro/auth.html"               200
check "robots.txt"           "https://findajob.pro/robots.txt"              200
check "branded 404"          "https://findajob.pro/nope"                    404
check "www redirect"         "https://www.findajob.pro/"                    308
# The leak pin. It was 200 on 2026-08-02; it must never be again.
check "graphify not public"  "https://findajob.pro/graphify-out/graph.json" 404

if ! curl -sI --max-time 15 https://findajob.pro/ | grep -qi 'content-security-policy'; then
    printf '    \033[31mFAIL\033[0m %-34s header missing\n' "CSP present"
    fail=$((fail + 1))
else
    printf '    \033[32mok\033[0m   %-34s present\n' "CSP present"
fi

if [ "$fail" -gt 0 ]; then
    printf '\n\033[31m%s check(s) failed.\033[0m Roll back with:\n  %s\n' "$fail" "$ROLLBACK" >&2
    exit 1
fi

# --- 7. record ----------------------------------------------------------------
# The box is not a git repo; without this there is no way to ask it what it is
# running.

ssh "$HOST" "printf '%s %s %s\n' '$TS' '$SHA' '$TARGET' >> '$ROOT/deploy.log'
             printf '%s\n' '$SHA' > '$ROOT/DEPLOYED_SHA'"

say "deployed $SHORT"
note "rollback, if you need it later: $ROLLBACK"
