#!/usr/bin/env bash
# =============================================================================
# findajob.pro — deploy to the VPS.
#
#   deploy/deploy.sh web  [--dry-run] [--yes] [--skip-ci-check]
#   deploy/deploy.sh api  [--dry-run] [--yes] [--skip-ci-check] [--allow-migrations]
#
# `nginx` and `traefik` are still manual — see deploy/README.md. They are the
# two that can take the whole site down (the nginx.conf inode trap and Traefik's
# unreliable file watcher), and neither has been exercised enough here to
# automate honestly.
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
# that is not committed. It also makes the README's exclude list obsolete —
# .venv/, __pycache__/, .env, staticfiles/, media/ and db.sqlite3 are all
# gitignored, so `git archive` never emits them and no list has to be kept in
# sync with reality.
#
# UI-FIRST holds throughout: `web` has no depends_on, so building, migrating or
# recreating the api never takes the interface down. The verification step
# asserts that rather than assuming it.
#
# Runs on the LAPTOP (macOS: bash 3.2, openrsync). No bash 4 syntax.
# =============================================================================

set -euo pipefail

HOST="${FINDAJOB_HOST:-macbook-air}"
ROOT="${FINDAJOB_ROOT:-/root/findajob}"
KEEP_SNAPSHOTS=5
KEEP_IMAGE_TAGS=5

# Applied to `migrate` only. lock_timeout is the important one: an ALTER TABLE
# that cannot get its lock will otherwise queue behind a long read AND block
# every request arriving after it — the classic "one migration took the site
# down at 23:00". Django wraps migrations in a transaction on PostgreSQL, so
# hitting the timeout rolls back cleanly rather than leaving half a schema.
# Raise MIGRATE_STATEMENT_TIMEOUT for a genuinely long index build.
MIGRATE_LOCK_TIMEOUT="${MIGRATE_LOCK_TIMEOUT:-3s}"
MIGRATE_STATEMENT_TIMEOUT="${MIGRATE_STATEMENT_TIMEOUT:-60s}"

TARGET=""
DRY_RUN=0
ASSUME_YES=0
SKIP_CI=0
ALLOW_MIGRATIONS=0

die()  { printf 'deploy.sh: %s\n' "$*" >&2; exit 1; }
say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf '    \033[33m!\033[0m  %s\n' "$*"; }

usage() {
    sed -n '2,33p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        web|api)            TARGET="$1"; shift ;;
        nginx|traefik)      die "target '$1' is not implemented — do it by hand, see deploy/README.md. These two can take the whole site down." ;;
        --dry-run)          DRY_RUN=1; shift ;;
        --yes|-y)           ASSUME_YES=1; shift ;;
        --skip-ci-check)    SKIP_CI=1; shift ;;
        --allow-migrations) ALLOW_MIGRATIONS=1; shift ;;
        -h|--help)          usage 0 ;;
        *)                  printf 'deploy.sh: unknown argument %s\n\n' "$1" >&2; usage 2 ;;
    esac
done
[ -n "$TARGET" ] || { printf 'deploy.sh: no target given\n\n' >&2; usage 2; }

cd "$(git rev-parse --show-toplevel)" || die "not inside the repo"

SHA="$(git rev-parse HEAD)"
SHORT="$(git rev-parse --short=12 HEAD)"
TS="$(date -u +%Y%m%d_%H%M%S)"

case "$TARGET" in
    web) PATHS="web" ;;
    api) PATHS="backend deploy" ;;
esac

# =============================================================================
# 1. preflight — a refusal to deploy anything you cannot later identify
# =============================================================================

say "preflight ($TARGET, $SHORT)"

# The payload comes from HEAD, so an uncommitted edit would silently NOT ship.
# Failing loudly beats deploying yesterday's file while looking at today's.
# shellcheck disable=SC2086
git diff --quiet --ignore-submodules -- $PATHS \
    || die "$PATHS has uncommitted changes — the payload comes from HEAD, so they would not ship. Commit or stash them."
# shellcheck disable=SC2086
git diff --cached --quiet --ignore-submodules -- $PATHS \
    || die "$PATHS has staged-but-uncommitted changes — same problem. Commit them."

# /root/findajob is NOT a git repo (deploy/README.md §8 documents a
# `git -C /root/findajob checkout` that cannot possibly run). So the pushed
# remote is the only durable record of what is on the box.
git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1 || die "no upstream branch configured"
[ "$(git rev-list --count '@{u}..HEAD')" = 0 ] \
    || die "HEAD is not pushed. /root/findajob is not a git repo, so the remote is the only record of what is deployed. Push first."

# CI gate. An EMPTY check-runs array is treated as failure, not as "nothing to
# see": push-then-immediately-deploy is the most common case, and there the runs
# have not registered yet — exactly when you least want a silent pass.
#
# `api` additionally requires migrations-pg16, because the suite runs on SQLite
# and this target is the one that can put a migration into PostgreSQL.
ci_green() {
    gh api "repos/{owner}/{repo}/commits/$SHA/check-runs" \
        --jq "[.check_runs[] | select(.name == \"$1\" and .conclusion == \"success\")] | length" 2>/dev/null || echo 0
}
if [ "$SKIP_CI" -eq 0 ]; then
    [ "$(ci_green backend)" -ge 1 ] \
        || die "CI is not green for $SHORT (job 'backend' has no successful run). Wait for it, or --skip-ci-check during an incident."
    note "CI: backend green"
    if [ "$TARGET" = api ]; then
        [ "$(ci_green migrations-pg16)" -ge 1 ] \
            || die "CI job 'migrations-pg16' is not green for $SHORT. The suite runs on SQLite; this is the only check that a migration is valid on PostgreSQL 16. Do not deploy the api without it."
        note "CI: migrations-pg16 green"
    fi
else
    warn "CI gate SKIPPED (--skip-ci-check)"
fi

# =============================================================================
# 2. payload from git, never from the working tree
# =============================================================================

say "payload"

PAYLOAD="$(mktemp -d)"
trap 'rm -rf "$PAYLOAD"' EXIT
# shellcheck disable=SC2086
git archive --format=tar "$SHA" $PATHS | tar -x -C "$PAYLOAD"
note "$(find "$PAYLOAD" -type f | wc -l | tr -d ' ') tracked files from $SHORT"

# The pin for the incident that motivated this script. CI asserts the same
# thing; belt and braces, because CI can be skipped and this cannot.
if [ -d "$PAYLOAD/web/graphify-out" ]; then
    die "web/graphify-out/ is in the payload — that is the internal codebase map and must never be published. Remove it from git."
fi

# =============================================================================
# 3. target: web
# =============================================================================

deploy_web() {
    if [ "$DRY_RUN" -eq 1 ]; then
        say "dry run — what WOULD change on $HOST:$ROOT/web/"
        rsync -rlptDn --delete --itemize-changes "$PAYLOAD/web/" "$HOST:$ROOT/web/" \
            || die "rsync dry run failed"
        note "nothing was written."
        return 0
    fi

    confirm "Deploy web/ at $SHORT to $HOST:$ROOT ?"

    # Order is load-bearing: ./web is a DIRECTORY bind mount, so the rsync below
    # IS the application — there is no separate apply step. A snapshot taken
    # afterwards would photograph the already-broken state.
    #
    # `cp -al` hardlinks: costs inodes, no data. It stays correct because rsync
    # writes a temp file and renames it, breaking the link rather than editing
    # the snapshot in place. Do NOT add --inplace; it would corrupt every
    # snapshot at once.
    say "snapshot"
    ssh "$HOST" "set -e
        mkdir -p '$ROOT/.deploy-state'
        rm -rf '$ROOT/.deploy-state/web-$TS'
        cp -al '$ROOT/web' '$ROOT/.deploy-state/web-$TS'
        sha256sum '$ROOT/web/legal.js' | awk '{print \$1}' > '$ROOT/.deploy-state/legal-$TS.sha256' || true
        ls -1dt '$ROOT'/.deploy-state/web-* 2>/dev/null | tail -n +$((KEEP_SNAPSHOTS + 1)) | xargs -r rm -rf"
    note "web-$TS (keeping $KEEP_SNAPSHOTS)"

    # Restores INTO $ROOT/web rather than replacing the directory, for the same
    # reason the transfer below does: ./web is a directory bind mount and Docker
    # resolved it at container CREATE. `mv $ROOT/web …` renames the very inode
    # nginx is serving — the container follows the rename and keeps serving the
    # failed payload, while the freshly created $ROOT/web is a different inode it
    # never sees. The rollback would report success and change nothing visible,
    # which is the exact trap this file warns about three lines down.
    #
    # `-c` (checksum) rather than the default size+mtime quick check: a rollback
    # is the one moment where being right matters more than being fast, the
    # webroot is under a megabyte, and a payload that differs from the snapshot
    # only in content — same size, same mtime — is precisely the sort of thing
    # one would be rolling back from.
    #
    # The failed payload is hardlinked aside first, so it can still be examined.
    # `;` not `&&`: forensics must never be able to block the restore. And it is
    # a timestamped name under .deploy-state, not a fixed $ROOT/web.failed —
    # the old fixed name meant a SECOND rollback began with `rm -rf` of the
    # directory the container was by then actually serving.
    ROLLBACK="ssh $HOST \"cp -al $ROOT/web $ROOT/.deploy-state/failed-$TS 2>/dev/null; rsync -rlptDc --delete $ROOT/.deploy-state/web-$TS/ $ROOT/web/\""

    # -rlptD, not -a: -a additionally carries owner and group, which is how every
    # file in the webroot ended up owned by 501:staff — the laptop's uid.
    # --delay-updates: rsync 3.x deletes DURING the transfer by default, so a
    # network drop halfway leaves the live site missing files.
    # NOT a directory `mv`: Docker resolves a bind mount at container CREATE.
    say "transfer"
    rsync -rlptD --delete --delay-updates "$PAYLOAD/web/" "$HOST:$ROOT/web/" \
        || die "rsync failed — the site may be mid-update. Roll back with:
  $ROLLBACK"

    # legal.js is both what the browser renders and what `seed_legal` hashes into
    # LegalDocument. Consent resolves server-side at verify time and fails CLOSED
    # with 503 when unseeded, so a changed legal.js without a re-seed breaks
    # first login.
    #
    # The comparison happens HERE, not over ssh: `cmp -s <(…)` is process
    # substitution, which is bash-only, and the remote command runs in root's
    # login shell. Under dash it would fail — and `if !` would read that failure
    # as "legal.js changed" and re-seed on every single deploy.
    # The seed runs UNCONDITIONALLY, and that is the fix for a silent-skip bug:
    # the decision used to be "did this rsync change legal.js", which is not the
    # question. The question is "does the database match the file now being
    # served". Those differ exactly when it matters most — after a failed seed.
    # `seed_legal` died, the operator followed the message and re-ran the deploy,
    # and on that second run the live file was ALREADY the new one, so the hashes
    # matched, the seed was skipped, and the deploy reported success while prod
    # served the new AGB against the old hashes. Permanently, and silently.
    #
    # Unconditional is affordable because the command is idempotent by design
    # (see seed_legal.py): with nothing to do it writes nothing and reports
    # "0 created, 18 unchanged".
    LEGAL_NOW="$(ssh "$HOST" "sha256sum '$ROOT/web/legal.js' 2>/dev/null | awk '{print \$1}'")"
    LEGAL_WAS="$(ssh "$HOST" "cat '$ROOT/.deploy-state/legal-$TS.sha256' 2>/dev/null || true")"
    LEGAL_CHANGED=0
    if [ -z "$LEGAL_NOW" ] || [ "$LEGAL_NOW" != "$LEGAL_WAS" ]; then LEGAL_CHANGED=1; fi

    say "seed legal"
    if seed_out="$(ssh "$HOST" "cd '$ROOT' && docker compose exec -T api python manage.py seed_legal" 2>&1)"; then
        printf '%s\n' "$seed_out" | tail -1 | sed 's/^/    /'
    elif [ "$LEGAL_CHANGED" -eq 1 ]; then
        # The served text moved and the hashes did not follow it. Consent now
        # resolves against a document nobody is reading.
        printf '%s\n' "$seed_out" | tail -10 | sed 's/^/    /'
        die "seed_legal FAILED and legal.js CHANGED in this deploy — the served text and the
hashed text now disagree. Consent fails closed with 503 when unseeded, and
records against the wrong hash when stale. Fix it and re-run (the seed is
unconditional now, so a re-run really does re-seed), or roll back:
  $ROLLBACK"
    else
        # legal.js is byte-identical to what was already seeded, so consent is
        # still resolving against the right text. Worth saying out loud — it
        # usually means the api container is down — but not worth failing a
        # web-only deploy that is otherwise correct.
        printf '%s\n' "$seed_out" | tail -5 | sed 's/^/    /'
        warn "seed_legal did not run, but legal.js is unchanged from what is already"
        warn "seeded, so consent still resolves correctly. Check the api container."
    fi
}

# =============================================================================
# 4. target: api
# =============================================================================

deploy_api() {
    if [ "$DRY_RUN" -eq 1 ]; then
        say "dry run — what WOULD change on $HOST:$ROOT/backend/"
        rsync -rlptDn --delete --itemize-changes "$PAYLOAD/backend/" "$HOST:$ROOT/backend/" \
            || die "rsync dry run failed"
        say "dry run — compose file"
        rsync -rlptDn --itemize-changes "$PAYLOAD/deploy/docker-compose.yml" "$HOST:$ROOT/docker-compose.yml"
        say "dry run — pending migrations on the LIVE database"
        ssh "$HOST" "cd '$ROOT' && docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate --plan" 2>&1 | tail -20
        note "nothing was written. NOTE: the plan above came from the image currently"
        note "running, not from $SHORT — build first to see the new one."
        return 0
    fi

    confirm "Deploy api at $SHORT to $HOST:$ROOT ?"

    # --- rollback target BEFORE anything else --------------------------------
    # `findajob-api:local` is what compose builds into, so it is a MUTABLE tag:
    # the build below destroys the only reference to the running image. Capture
    # it first or there is nothing to go back to.
    say "tag current image"
    ssh "$HOST" "set -e
        cd '$ROOT'
        docker tag findajob-api:local findajob-api:rollback-$TS
        docker images --format '{{.Repository}}:{{.Tag}}' \
            | grep '^findajob-api:rollback-' | sort -r | tail -n +$((KEEP_IMAGE_TAGS + 1)) \
            | xargs -r -n1 docker rmi -f >/dev/null 2>&1 || true"
    note "findajob-api:rollback-$TS"

    ROLLBACK="ssh $HOST \"cd $ROOT && docker tag findajob-api:rollback-$TS findajob-api:local && docker compose up -d api\""

    # --- ship the build context ----------------------------------------------
    # The compose file lives at the repo ROOT on the VPS, not in deploy/ — every
    # relative path inside it is written for that location.
    # What the RUNNING container is serving, read before anything is copied.
    #
    # Compared against the container rather than against the file on disk, and
    # that is the whole point: Docker binds deploy/nginx.conf by INODE, and
    # rsync writes a temp file and renames it. So the on-disk copy can already
    # match the payload while nginx keeps serving the previous config —
    # `nginx -t` and `nginx -s reload` both report success while changing
    # nothing. Comparing the two files on disk would find them equal and say
    # everything is fine in exactly the case that is broken.
    NGINX_SERVED="$(ssh "$HOST" "cd '$ROOT' && docker compose exec -T web sha256sum /etc/nginx/conf.d/default.conf 2>/dev/null | awk '{print \$1}'" || true)"
    NGINX_SHIPPING="$(shasum -a 256 "$PAYLOAD/deploy/nginx.conf" | awk '{print $1}')"

    say "transfer"
    rsync -rlptD --delete --delay-updates "$PAYLOAD/backend/" "$HOST:$ROOT/backend/"
    rsync -rlptD "$PAYLOAD/deploy/docker-compose.yml" "$HOST:$ROOT/docker-compose.yml"
    rsync -rlptD "$PAYLOAD/deploy/nginx.conf" "$HOST:$ROOT/deploy/nginx.conf"
    rsync -rlptD "$PAYLOAD/deploy/cron/" "$HOST:$ROOT/deploy/cron/"
    note "backend/, docker-compose.yml, nginx.conf, cron scripts"

    # Only speak up when there is something to do. An unconditional warning is
    # one people learn to scroll past, and then it is not a warning.
    if [ -z "$NGINX_SERVED" ]; then
        warn "could not read the running nginx config — check whether nginx.conf needs applying by hand."
    elif [ "$NGINX_SERVED" != "$NGINX_SHIPPING" ]; then
        warn "nginx.conf CHANGED and is NOT live yet. It is bind-mounted by inode, so"
        warn "a reload is not enough — 'nginx -t' and 'nginx -s reload' will both"
        warn "report success while still serving the old file. Apply it with:"
        warn "    ssh $HOST \"cd $ROOT && docker compose up -d --force-recreate web\""
        warn "That briefly 502s the site (~1-2s), which is why it is not automatic."
    fi

    # --- build ----------------------------------------------------------------
    # A build never touches the running container.
    say "build"
    ssh "$HOST" "cd '$ROOT' && docker compose build api" \
        || die "build FAILED. Nothing was changed — the old container is still running and still serving."
    ssh "$HOST" "cd '$ROOT' && docker tag findajob-api:local findajob-api:sha-$SHORT"
    note "built, tagged findajob-api:sha-$SHORT"

    # --- migrations -----------------------------------------------------------
    # The entrypoint deliberately never migrates (backend/docker/entrypoint.sh
    # explains why: one migration per replica racing the same lock, and
    # "restart the API" silently meaning "mutate the production schema").
    #
    # `migrate --check` exits non-zero when anything is unapplied, so this is a
    # real gate rather than output parsing. It runs from the image just built,
    # which is what makes it the NEW code's plan.
    say "migrations"
    mig_rc=0
    mig_out="$(ssh "$HOST" "cd '$ROOT' && docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate --check" 2>&1)" || mig_rc=$?
    if [ "$mig_rc" -eq 0 ]; then
        note "none pending"
    else
        # `migrate --check` exits non-zero for TWO different reasons: unapplied
        # migrations, and Django failing to start at all. Reading every non-zero
        # as "pending" told the operator the deploy would change the production
        # schema — and then printed an empty plan, because the same crash also
        # killed `--plan`, whose stderr used to go to /dev/null. Under
        # --allow-migrations that path continued into a pg_dump and a "Apply the
        # migrations above?" prompt with nothing above it.
        #
        # Told apart by whether a plan actually came back: Django prints
        # `[ ] app.0001_name` per pending migration, and we only get here when
        # --check already said something is pending, so an empty plan is a crash.
        plan_rc=0
        plan_out="$(ssh "$HOST" "cd '$ROOT' && docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate --plan" 2>&1)" || plan_rc=$?
        if [ "$plan_rc" -ne 0 ] || ! printf '%s' "$plan_out" | grep -q '\[ \]'; then
            printf '%s\n' "$mig_out" | tail -20 | sed 's/^/    /'
            die "could not read a migration plan from the image just built. That is Django
failing to start, not a pending schema change — the output above is the real
error. The old container is still running and still serving; nothing changed."
        fi

        say "PENDING MIGRATIONS"
        printf '%s\n' "$plan_out" | sed 's/^/    /'

        [ "$ALLOW_MIGRATIONS" -eq 1 ] \
            || die "the deploy would change the production schema. Re-run with --allow-migrations once you have read the plan above.

A migration is the one step with no rollback: reverting code is retagging an
image, reverting a schema means restoring a dump and LOSING every write made
since it was taken. Prefer additive migrations (nullable columns now, drops in
a later release)."

        # Dump FIRST, and use the verified runner rather than a bare pg_dump —
        # it checks size, gzip integrity and content before reporting success,
        # so "the backup exists" is not confused with "a file exists".
        # Captured rather than piped. `ssh … | tail || die` would also be correct
        # HERE, because `set -o pipefail` is on at the top of this file — but it
        # is correct only because of that, and it prints the runner's whole block
        # on success. Capturing keeps the good path to one DB-BACKUP line and the
        # failure path to the actual error.
        say "pre-migration backup"
        if backup_out="$(ssh "$HOST" "'$ROOT/deploy/cron/run-db-backup.sh'" 2>&1)"; then
            printf '%s\n' "$backup_out" | grep '^DB-BACKUP' | sed 's/^/    /'
        else
            printf '%s\n' "$backup_out" | tail -10 | sed 's/^/    /'
            die "pre-migration backup FAILED — refusing to migrate without one."
        fi

        confirm "Apply the migrations above to the PRODUCTION database?"

        say "migrate"
        ssh "$HOST" "cd '$ROOT' && docker compose run --rm \
            -e DJANGO_COLLECTSTATIC=0 \
            -e PGOPTIONS='-c lock_timeout=$MIGRATE_LOCK_TIMEOUT -c statement_timeout=$MIGRATE_STATEMENT_TIMEOUT' \
            api python manage.py migrate --no-input" \
            || die "MIGRATION FAILED. Django wraps migrations in a transaction on PostgreSQL, so a
lock_timeout or statement_timeout abort rolled back cleanly and the schema is
unchanged — check the error above before assuming otherwise. The NEW image is
built but NOT running; the old container is still serving. Either fix and
re-run, or discard the build with:
  $ROLLBACK"
        note "applied with lock_timeout=$MIGRATE_LOCK_TIMEOUT statement_timeout=$MIGRATE_STATEMENT_TIMEOUT"
    fi

    # --- start ----------------------------------------------------------------
    # `up -d`, NEVER `restart`: container environment and mounts are fixed at
    # CREATE time, so a bare restart re-runs the OLD image with the OLD env.
    say "start"
    ssh "$HOST" "cd '$ROOT' && docker compose up -d api" \
        || die "compose up FAILED. Roll back with:
  $ROLLBACK"

    # The image declares a HEALTHCHECK, so ask Docker rather than guessing with
    # a sleep. The entrypoint runs collectstatic, which runs Django's system
    # checks — findajob.E001/E003/E005/E006 abort the container instead of
    # serving a UI that mails nobody. That failure shows up here as `unhealthy`
    # or as a container that is not running at all.
    say "wait for healthy"
    waited=0
    while [ "$waited" -lt 90 ]; do
        state="$(ssh "$HOST" "docker inspect -f '{{.State.Health.Status}}' findajob-api 2>/dev/null || echo missing")"
        case "$state" in
            healthy) note "healthy after ${waited}s"; break ;;
            unhealthy|missing)
                ssh "$HOST" "cd '$ROOT' && docker compose logs --tail 40 api" 2>&1 | sed 's/^/    /'
                die "container is $state. Roll back with:
  $ROLLBACK" ;;
        esac
        sleep 5
        waited=$((waited + 5))
    done
    [ "$waited" -lt 90 ] || die "still not healthy after 90s. Roll back with:
  $ROLLBACK"
}

# =============================================================================
# helpers
# =============================================================================

confirm() {
    # `if`, not `[ … ] && return 0`. Both work — bash exempts the left operand
    # of && from `set -e`, verified — but the short form's correctness depends
    # on knowing that exemption, and it silently becomes a bug the day someone
    # writes `[ … ] && something` where the intent was a plain guard.
    if [ "$ASSUME_YES" -eq 1 ]; then return 0; fi
    printf '%s [y/N] ' "$1"
    read -r reply
    case "$reply" in y|Y|yes) return 0 ;; *) die "aborted" ;; esac
}

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

# =============================================================================
# run
# =============================================================================

case "$TARGET" in
    web) deploy_web ;;
    api) deploy_api ;;
esac
if [ "$DRY_RUN" -eq 1 ]; then exit 0; fi

# --- verify -------------------------------------------------------------------
# Read the result back rather than trusting an exit code. Every trap on this box
# is of the form "the tool reports success and changes nothing".

say "verify"

# UI-FIRST, asserted on BOTH targets: an api deploy must never take the
# interface down. web has no depends_on, so this should hold — this is where we
# find out it still does.
check "landing"              "https://findajob.pro/"                        200
check "dashboard"            "https://findajob.pro/app.html"                200
check "auth"                 "https://findajob.pro/auth.html"               200
check "branded 404"          "https://findajob.pro/nope"                    404
# The leak pin. It was 200 on 2026-08-02; it must never be again.
check "graphify not public"  "https://findajob.pro/graphify-out/graph.json" 404
# Asserted on BOTH targets, because `web` is the target that ships legal.js and
# therefore the one that can leave the corpus unseeded. 503 here is the
# fail-closed path in apps.legal, and it means nobody can complete a first
# login. (200 proves the corpus is seeded, not that it is CURRENT — the seed
# step above is what establishes that, and it is unconditional for this reason.)
check "legal (seeded)"       "https://findajob.pro/api/v1/legal/"           200

if [ "$TARGET" = api ]; then
    # 401 not 403: the session endpoint must reject an anonymous caller, and a
    # 502 here means nginx cannot reach the container at all.
    check "api session gate"  "https://findajob.pro/api/v1/me/"    401
    check "admin basicAuth"   "https://findajob.pro/admin/"        401
fi

if ! curl -sI --max-time 15 https://findajob.pro/ | grep -qi 'content-security-policy'; then
    printf '    \033[31mFAIL\033[0m %-34s header missing\n' "CSP present"
    fail=$((fail + 1))
else
    printf '    \033[32mok\033[0m   %-34s present\n' "CSP present"
fi

# The deep gate, api only: 12 steps through the real login flow over real HTTPS.
# It is a GATE, not an auto-rollback trigger — it has a 60s per-address cooldown
# and asserts things (Brevo, DNS) that can be red for reasons this deploy did
# not cause. Reverting on its word would revert healthy deploys.
if [ "$TARGET" = api ] && [ "$fail" -eq 0 ]; then
    say "login flow"
    # Captured rather than piped, same reasoning as the backup above: `pipefail`
    # makes the piped form work too, but capturing lets the success path print 3
    # lines and the failure path print 20 — which is the difference between a
    # readable deploy log and one nobody reads.
    if lf_out="$(ssh "$HOST" "cd '$ROOT' && docker compose exec -T api python manage.py check_login_flow" 2>&1)"; then
        printf '%s\n' "$lf_out" | tail -3 | sed 's/^/    /'
    else
        printf '%s\n' "$lf_out" | tail -20 | sed 's/^/    /'
        fail=$((fail + 1))
    fi
fi

if [ "$fail" -gt 0 ]; then
    printf '\n\033[31m%s check(s) failed.\033[0m Roll back with:\n  %s\n' "$fail" "${ROLLBACK:-<no rollback recorded>}" >&2
    exit 1
fi

# --- record -------------------------------------------------------------------
# The box is not a git repo; without this there is no way to ask it what it runs.

ssh "$HOST" "printf '%s %s %s\n' '$TS' '$SHA' '$TARGET' >> '$ROOT/deploy.log'
             printf '%s\n' '$SHA' > '$ROOT/DEPLOYED_SHA'"

say "deployed $TARGET at $SHORT"
note "rollback, if you need it later: ${ROLLBACK:-<none>}"
