#!/bin/sh
# =============================================================================
# findajob.pro - login healthcheck runner.
#
# Repo copy:  deploy/cron/run-login-check.sh
# VPS path:   /root/findajob/deploy/cron/run-login-check.sh   (mode 0755)
# Invoked by: /etc/cron.d/findajob-login-check, under `flock -n`.
#
# Runs `manage.py check_login_flow` inside the api container and renders ONE
# chronological block on stdout. It writes to no file: the cron line appends
# stdout to /var/log/findajob/login-check.log, so this script can be run by hand
# at any time and will print to the terminal without touching the record.
#
# The only contract it depends on is the command's:
#     exit 0 = pass, non-zero = fail, `--json` available for machine output.
# Everything the command prints is reproduced verbatim inside the block, so this
# wrapper never has to be updated when a step is added or renamed.
#
# Read docs/2026-08-02-login-healthcheck.md for what a PASS actually proves
# (short version: Brevo ACCEPTED the message; a human receiving it is a
# different and unverified claim).
#
# POSIX sh only - no bashisms. /bin/sh on this box is dash.
# =============================================================================

set -u

# --- configuration ------------------------------------------------------------
# All overridable from the environment so a hand-run can vary one thing without
# editing the file. Defaults are the production values.

# Compose project directory. `docker compose` resolves the project from the cwd,
# and cron's cwd is the invoking user's home, so this cd is mandatory.
PROJECT_DIR="${LOGIN_CHECK_PROJECT_DIR:-/root/findajob}"

# Compose SERVICE name (not the container name). `docker compose exec` addresses
# services; `findajob-api` is the container_name and would not resolve here.
SERVICE="${LOGIN_CHECK_SERVICE:-api}"

# The management command. Owned by apps/common; see the doc.
COMMAND="${LOGIN_CHECK_COMMAND:-check_login_flow}"

# Extra arguments passed straight through, e.g.:
#     LOGIN_CHECK_ARGS=--json ./run-login-check.sh
# Left empty in cron: the human-readable output is what makes the log readable
# months later, and --json would reduce the block to one unreadable line.
LOGIN_CHECK_ARGS="${LOGIN_CHECK_ARGS:-}"

# Hard ceiling on the whole check, in seconds.
#
# Why it exists AND why it is inside the lock rather than on the cron line:
# `flock -n` guarantees runs never overlap, but it cannot bound how long one run
# holds the lock. A single hung run without a timeout holds it FOREVER, and
# every subsequent tick is then skipped silently - a worse failure than the hang
# itself, because the log fills with "cannot acquire lock" and nobody is
# monitoring a monitor. Bounding the only blocking call here means this script
# always returns, so the lock is always released.
#
# 180 s is generous by design: the OTP send path bounds itself at
# EMAIL_TIMEOUT=5 s per socket operation, and a healthy run finishes in single
# digits. Anything approaching 180 s is already a failure worth recording.
#
# RESIDUAL, and it is real: `timeout` kills the docker CLI on the host. Recent
# Compose forwards SIGTERM to the in-container process, but that is not
# guaranteed for a process blocked in a syscall - a truly stuck check may keep
# running inside the container after this script has given up. It cannot block
# the next tick (the lock is released), but if you see repeated timeouts, look
# for orphaned `manage.py check_login_flow` processes with
#     docker compose exec api ps -ef | grep check_login_flow
TIMEOUT_SECONDS="${LOGIN_CHECK_TIMEOUT:-180}"

# --- identity of this run -----------------------------------------------------
# RUN_ID correlates the block header with the summary line at the bottom of the
# block, and survives interleaving if a future change ever removes the lock.
# Compact UTC so it sorts lexicographically = chronologically.
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)"
# Unambiguous wall-clock stamp WITH the offset. `date -Is` on GNU coreutils
# yields e.g. 2026-08-02T06:17:03+00:00. Never log a bare local time: cron runs
# in system local time, the container runs in UTC, and a reader in six months
# has no way to reconcile two unlabelled clocks.
STARTED_AT="$(date -Is 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)"
STARTED_EPOCH="$(date -u +%s)"

# Was this a scheduled run or a human? cron gives no TTY.
if [ -t 1 ]; then INVOKER="manual (tty)"; else INVOKER="cron"; fi

# --- run it -------------------------------------------------------------------
# ASCII only, on purpose. This log is read months later over ssh, sometimes
# under a C/POSIX locale; a decorative non-ASCII separator turns into mojibake
# there and, worse, breaks a `grep` written against the pretty version.
printf '=== findajob login healthcheck | run %s | %s ===\n' "$RUN_ID" "$STARTED_AT"
printf '    project     : %s\n' "$PROJECT_DIR"
printf '    target      : compose service %s (container findajob-api)\n' "$SERVICE"
printf '    command     : manage.py %s%s\n' "$COMMAND" \
    "$( [ -n "$LOGIN_CHECK_ARGS" ] && printf ' %s' "$LOGIN_CHECK_ARGS" )"
printf '    invoked by  : %s\n' "$INVOKER"
printf '    timeout     : %ss\n' "$TIMEOUT_SECONDS"

OUTPUT=''
EXIT_CODE=0

if ! cd "$PROJECT_DIR" 2>/dev/null; then
    OUTPUT="run-login-check.sh: cannot cd to ${PROJECT_DIR} - is the project still deployed there?"
    EXIT_CODE=127
else
    # `docker compose exec -T`:
    #   exec  - reuse the RUNNING api container. `run` would start a SECOND
    #           container: slower, and it would exercise a fresh process rather
    #           than the environment real logins actually run in. Testing the
    #           live container is the entire point.
    #   -T    - disable pseudo-TTY allocation. cron has no TTY and Compose
    #           errors out with "the input device is not a TTY" without it. This
    #           single flag is the most common reason a cron'd docker exec works
    #           interactively and fails on schedule.
    #
    # stderr is folded into stdout (2>&1) so a Python traceback lands inside the
    # run's block instead of jumping the queue through cron's own redirection.
    #
    # Note the quoting: $LOGIN_CHECK_ARGS is deliberately UNQUOTED so that
    # multiple flags word-split into separate arguments. It is operator input
    # from the environment, never user input.
    # shellcheck disable=SC2086
    OUTPUT="$(timeout "$TIMEOUT_SECONDS" docker compose exec -T "$SERVICE" \
        python manage.py "$COMMAND" $LOGIN_CHECK_ARGS 2>&1)"
    EXIT_CODE=$?
fi

FINISHED_EPOCH="$(date -u +%s)"
DURATION=$((FINISHED_EPOCH - STARTED_EPOCH))

# `timeout` reports 124 when it fired. Say so explicitly - otherwise a timeout
# looks like an ordinary command failure and sends the next reader down the
# wrong path entirely (they will go hunting in Brevo instead of at the socket).
if [ "$EXIT_CODE" -eq 124 ]; then
    OUTPUT="${OUTPUT}
run-login-check.sh: TIMED OUT after ${TIMEOUT_SECONDS}s - the check never returned.
run-login-check.sh: this is a hang (network, SMTP socket, or a wedged container),
run-login-check.sh: NOT a reported failure. See the doc, failure mode 'timeout'."
fi

# --- render the command's own output, indented into the block -----------------
printf -- '--- output -------------------------------------------------------------\n'
if [ -n "$OUTPUT" ]; then
    printf '%s\n' "$OUTPUT" | sed 's/^/    /'
else
    printf '    (no output)\n'
fi
printf -- '--- end output ---------------------------------------------------------\n'

# --- count the steps, tolerantly ---------------------------------------------
# The step counts are a CONVENIENCE for the one-line summary. The exit code is
# authoritative and always correct; the counts are best-effort because this
# wrapper deliberately does not couple itself to the command's exact formatting.
#
# Tier 0: the format `check_login_flow` ACTUALLY prints today, measured against
#         the live command on 2026-08-02:
#
#             2026-08-02T09:01:51Z  OK     0  preflight        91ms  ...
#             2026-08-02T08:59:49Z  FAIL   1  public-surfaces 152ms  ...
#
#         i.e. an ISO-8601 Z timestamp at column 1, then OK|FAIL, then the step
#         number. Anchored at `^` and requiring all three parts in order, so it
#         cannot match an evidence string that merely contains the word "FAIL".
#         Without this tier the summary line reported `steps=? failed=?` on every
#         single run — tier 1's brackets never appear, and tier 2 wants the token
#         at the start or the end of the line while this format puts it in the
#         middle. Measured; do not remove it on the theory that tier 2 covers it.
# Tier 1: bracketed markers, e.g. "[PASS] smtp_auth". Brackets are very unlikely
#         to collide with prose.
# Tier 2: a bare token at the start or end of a line, for a command that prints
#         "smtp_auth ... PASS".
# None matched: report "?" rather than a confident zero. A monitor that lies
# quietly is worse than one that admits it does not know. That is also why the
# tiers only ever ADD information — `exit=` stays authoritative either way.
STEPS='?'
FAILED='?'
if [ -n "$OUTPUT" ]; then
    _s_all="$(printf '%s\n' "$OUTPUT" | grep -Ec '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z[[:space:]]+(OK|FAIL)[[:space:]]+[0-9]+[[:space:]]' 2>/dev/null || true)"
    _s_bad="$(printf '%s\n' "$OUTPUT" | grep -Ec '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z[[:space:]]+FAIL[[:space:]]+[0-9]+[[:space:]]' 2>/dev/null || true)"
    _b_all="$(printf '%s\n' "$OUTPUT" | grep -Ec '\[(PASS|FAIL|SKIP|WARN|ERROR)\]' 2>/dev/null || true)"
    _b_bad="$(printf '%s\n' "$OUTPUT" | grep -Ec '\[(FAIL|ERROR)\]' 2>/dev/null || true)"
    if [ "${_s_all:-0}" -gt 0 ]; then
        STEPS="$_s_all"
        FAILED="${_s_bad:-0}"
    elif [ "${_b_all:-0}" -gt 0 ]; then
        STEPS="$_b_all"
        FAILED="${_b_bad:-0}"
    else
        _t_all="$(printf '%s\n' "$OUTPUT" | grep -Ec '(^[[:space:]]*(PASS|FAIL|SKIP|WARN|ERROR)\b|\b(PASS|FAIL|SKIP|WARN|ERROR)[[:space:]]*$)' 2>/dev/null || true)"
        _t_bad="$(printf '%s\n' "$OUTPUT" | grep -Ec '(^[[:space:]]*(FAIL|ERROR)\b|\b(FAIL|ERROR)[[:space:]]*$)' 2>/dev/null || true)"
        if [ "${_t_all:-0}" -gt 0 ]; then
            STEPS="$_t_all"
            FAILED="${_t_bad:-0}"
        fi
    fi
fi

# --- the one greppable line ---------------------------------------------------
# STABLE FORMAT. Anything may be APPENDED to it; nothing may be reordered or
# removed, because login-check-history and every future alert parses it by
# position and by key=value. Prefix is "LOGIN-CHECK " at column 1.
#
#   LOGIN-CHECK <iso8601-with-offset> <PASS|FAIL> duration=<n>s exit=<n> \
#               steps=<n|?> failed=<n|?> run=<id> invoked=<cron|manual>
#
# The verdict is derived from the EXIT CODE and from nothing else.
if [ "$EXIT_CODE" -eq 0 ]; then VERDICT='PASS'; else VERDICT='FAIL'; fi
case "$INVOKER" in manual*) INV='manual' ;; *) INV='cron' ;; esac

printf 'LOGIN-CHECK %s %s duration=%ss exit=%s steps=%s failed=%s run=%s invoked=%s\n' \
    "$STARTED_AT" "$VERDICT" "$DURATION" "$EXIT_CODE" "$STEPS" "$FAILED" "$RUN_ID" "$INV"
printf '\n'

# Propagate the verdict. Nothing consumes it under cron today, but it means the
# script composes correctly in a pipeline, in a manual `&& echo ok`, and in any
# future alerting wrapper.
exit "$EXIT_CODE"
