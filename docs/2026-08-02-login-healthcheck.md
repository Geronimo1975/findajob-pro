# Login healthcheck — scheduling, log, and how to read it

**Status:** written 2026-08-02. Covers the *scheduling and record-keeping* half of the login
healthcheck. The management command itself (`apps/common/management/commands/check_login_flow.py`)
is documented by its own module docstring; this document assumes only its published interface:

| Contract | Value |
|---|---|
| Invocation | `python manage.py check_login_flow` inside the `api` container |
| Exit code | `0` = every step passed · non-zero = at least one step failed |
| Flags | `--json` for machine-readable output |

Everything here is deliberately decoupled from the command's *formatting*, so adding or renaming
a step never requires touching the cron, the log format, or the reader.

---

## 1. Why this exists at all

Login on findajob.pro is a 6-digit email OTP sent **synchronously inside the request**
(auth spec §1). That makes the mail path *the* login path: if Brevo stops accepting our
messages, `POST /api/v1/auth/request-code/` starts answering `503 delivery_unavailable` and
**nobody can sign in** — existing users included, because there are no passwords to fall back on.

Nothing else on the box notices:

- The api container's own Docker `HEALTHCHECK` is **liveness only, on purpose**. It must not
  depend on Postgres, Redis or the ESP, or a database blip would restart-loop the API and take
  the healthy routes down with it. It will happily report "healthy" while login is 100% broken.
- Traefik only knows the container answers on `:8000`.
- Uptime pings against `https://findajob.pro/` test nginx and a static file. Browsing is free on
  this site (`/`, `/app.html`, `/app.html?demo=1`, `/auth.html` never prompt), so the landing
  page is green during a total login outage.

Two failures are already scheduled to happen:

1. **The Brevo SMTP key "findajob.pro OTP login" expires 2027-08-01** and nothing warns you.
2. **SPF / DKIM / DMARC for `mail.findajob.pro` are hand-edited TXT/CNAME records at Hostinger.**
   An unauthenticated sending domain is accepted by SMTP and then dropped at delivery, so our own
   API sees a clean 202 while nothing arrives.

The healthcheck is the thing that turns both into a dated line in a log instead of a support
ticket.

---

## 2. What is tested — and what is emphatically NOT

### The honest boundary

> **A `PASS` proves that Brevo ACCEPTED the message. It does NOT prove that a human received it.**

Those are different claims, and the gap between them is exactly where this system has burned
time before. The SMTP conversation ends when the relay answers `250 Ok queued as …`. What
happens after that — reputation filtering, the recipient's spam folder, a silent drop because the
sending domain fails DMARC alignment — is invisible from inside our network. Brevo logs an
`Error` event *next to* the `Sent` one in that case, and our API never sees it.

Real delivery requires all of the following, and **none of them is verified by this check**:

- SPF, DKIM and DMARC live and aligned for `mail.findajob.pro` (they were live and verified on
  2026-08-02 — but they are hand-edited records in an external DNS panel);
- the sender `noreply@mail.findajob.pro` still verified in Brevo;
- the recipient's provider not quietly filtering us;
- the recipient address not having landed on `SuppressedAddress` after a bounce.

Where the check can, it should say so per step (e.g. `[SKIP] inbox_delivery`). Where it cannot,
this document is the record. **Do not turn a green history into "email is fine".** For actual
delivery, look at the Brevo transactional log and filter for `Delivered`, not for a 2xx from our
own API.

### The layers a run does cover

Exactly which steps run is the command's business, not this document's. What the *schedule* is
designed to catch, ordered from cheapest to most valuable:

| Layer | Failure it catches |
|---|---|
| Container | `findajob-api` gone, restart-looping, or a stale image after a rebuild |
| Database | Postgres unreachable → sessions cannot be written, so even a correct code fails |
| Redis | wrong host resolved (this already happened: a bare `redis` alias resolved to another stack's cache on the shared `edge` network, every rate-limit call failed AUTH, and verify-code fell closed into a permanent 429) |
| OTP issue | the `EmailOTP` write path, constraints and expiry arithmetic |
| SMTP auth | **the expiring key**, a rotated password, a changed relay host |
| SMTP accept | a malformed `DEFAULT_FROM_EMAIL` — Django 6 rejects an unquoted display name containing a period (`findajob.pro <noreply@…>` → `ValueError: period in 'phrase'`), which surfaces only as a 503 |

### Two things it must not do

- **It must not mint a session or create an account.** The check exercises the send path; it is
  not a login. Nothing about it should leave a usable credential behind.
- **It must not print a plaintext code.** The only place in this codebase where a code is ever
  emitted is `manage.py issue_login_code`, and that is deliberate and break-glass only. The log
  written here is world-adjacent (`0640 root:adm`) and is kept for a year.

---

## 3. The moving parts

| File (repo) | Installed as | What it is |
|---|---|---|
| `deploy/cron/findajob-login-check` | `/etc/cron.d/findajob-login-check` (root:root 0644) | the schedule |
| `deploy/cron/run-login-check.sh` | `/root/findajob/deploy/cron/run-login-check.sh` (0755) | the runner — renders one log block |
| `deploy/cron/login-check-history` | `/root/findajob/deploy/cron/login-check-history` (0755) | the reader |
| `deploy/cron/findajob-login-check.logrotate` | `/etc/logrotate.d/findajob-login-check` (root:root 0644) | rotation |
| — | `/var/log/findajob/login-check.log` | the record |
| — | `/var/lock/findajob-login-check.lock` | the flock |

**Schedule: 06:17 daily, server local time.** Daily rather than hourly because every run sends a
real message through Brevo — it consumes quota and ages sender reputation, and hourly buys ~23
extra emails a day for almost no extra signal. `:17` rather than `:00` to stay off the
top-of-hour stampede.

### Deploy

Nothing here is installed automatically. Files only; the main session deploys.

```bash
# runner + reader (they live under the project, which is where the compose file is)
ssh macbook-air "mkdir -p /root/findajob/deploy/cron /var/log/findajob"
rsync -az deploy/cron/run-login-check.sh deploy/cron/login-check-history \
      macbook-air:/root/findajob/deploy/cron/
ssh macbook-air "chmod 0755 /root/findajob/deploy/cron/run-login-check.sh \
                            /root/findajob/deploy/cron/login-check-history"

# schedule + rotation (system paths, and the installed names must contain no dot)
scp deploy/cron/findajob-login-check macbook-air:/etc/cron.d/findajob-login-check
scp deploy/cron/findajob-login-check.logrotate \
    macbook-air:/etc/logrotate.d/findajob-login-check
ssh macbook-air "chown root:root /etc/cron.d/findajob-login-check \
                                 /etc/logrotate.d/findajob-login-check \
              && chmod 0644 /etc/cron.d/findajob-login-check \
                            /etc/logrotate.d/findajob-login-check"
```

Then **verify both installs**, because both subsystems fail silently:

```bash
# cron ignores a cron.d file that is group/other-writable, or whose name has a dot in it.
# There is no error message for either.
ssh macbook-air "ls -l /etc/cron.d/findajob-login-check && run-parts --test /etc/cron.d"

# a syntax error in ONE logrotate file aborts the whole daily pass for every log on the box
ssh macbook-air "logrotate -d /etc/logrotate.d/findajob-login-check"

# force one real run and read it back
ssh macbook-air "/root/findajob/deploy/cron/run-login-check.sh"
```

`rsync` preserves the executable bit with `-a`; `scp` does not, hence the explicit `chmod`.

---

## 4. Running it by hand

The runner writes **only to stdout**. Run it interactively and it prints to your terminal
without touching the log — that is the point of the design, and it means you can debug freely.

```bash
ssh macbook-air

# exactly what cron does, minus the redirection and the lock
/root/findajob/deploy/cron/run-login-check.sh ; echo "exit=$?"

# machine-readable, straight from the command
LOGIN_CHECK_ARGS=--json /root/findajob/deploy/cron/run-login-check.sh

# the raw command, no wrapper at all
cd /root/findajob && docker compose exec -T api python manage.py check_login_flow

# append a hand-run to the real record (rare; the summary line records invoked=manual)
/root/findajob/deploy/cron/run-login-check.sh >> /var/log/findajob/login-check.log 2>&1
```

Environment overrides on the runner, all documented in the file itself:
`LOGIN_CHECK_PROJECT_DIR`, `LOGIN_CHECK_SERVICE`, `LOGIN_CHECK_COMMAND`, `LOGIN_CHECK_ARGS`,
`LOGIN_CHECK_TIMEOUT`.

**Reproduce cron's environment**, not your shell's — a job that works by hand and fails on
schedule is almost always a `PATH` or TTY problem:

```bash
env -i SHELL=/bin/sh PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  /bin/sh -c 'mkdir -p /var/log/findajob && flock -n /var/lock/findajob-login-check.lock \
  /root/findajob/deploy/cron/run-login-check.sh'
```

---

## 5. Reading the log

### One command

```bash
ssh macbook-air /root/findajob/deploy/cron/login-check-history            # last 20 runs
ssh macbook-air /root/findajob/deploy/cron/login-check-history --limit 7  # this week
ssh macbook-air /root/findajob/deploy/cron/login-check-history --failures # only failures, all history
ssh macbook-air /root/findajob/deploy/cron/login-check-history --since 2026-07-01
ssh macbook-air /root/findajob/deploy/cron/login-check-history --last     # full block, most recent run
ssh macbook-air /root/findajob/deploy/cron/login-check-history --run 20260802T061703Z
```

```
date       time      verdict   dur  steps        run id
2026-08-02 06:17:03  PASS       4s  7/7          20260802T061703Z
2026-08-03 06:17:02  PASS       4s  7/7          20260803T061702Z
2026-08-04 06:17:05  FAIL       6s  2/3          20260804T061705Z
---
3 runs shown, 1 failed. Last: FAIL at 2026-08-04 06:17:05.
Inspect one with: login-check-history --run <run id>
```

It reads the live log **and its rotated archives**, gzipped or not, under either logrotate naming
scheme, ordered oldest-first by mtime — so "was login working last month?" still answers
correctly the day after a rotation.

### The file itself

Each run appends one block. The header and the summary line share a `run id`, so a block is
always recoverable even if something ever interleaves.

```
=== findajob login healthcheck | run 20260802T061703Z | 2026-08-02T06:17:03+00:00 ===
    project     : /root/findajob
    target      : compose service api (container findajob-api)
    command     : manage.py check_login_flow
    invoked by  : cron
    timeout     : 180s
--- output -------------------------------------------------------------
    [PASS] container_up            findajob-api healthy, up 3 days
    [PASS] smtp_auth               smtp-relay.brevo.com:587 accepted credentials
    [PASS] smtp_accepted           250 Ok queued
    [SKIP] inbox_delivery          not verifiable from here - see doc
--- end output ---------------------------------------------------------
LOGIN-CHECK 2026-08-02T06:17:03+00:00 PASS duration=4s exit=0 steps=4 failed=0 run=20260802T061703Z invoked=cron
```

The last line is the **stable, greppable contract**:

```
LOGIN-CHECK <iso8601-with-offset> <PASS|FAIL> duration=<n>s exit=<n> steps=<n|?> failed=<n|?> run=<id> invoked=<cron|manual>
```

Fields may be **appended**; nothing may be reordered or removed, because `login-check-history`
and any future alerting parse it. Notes:

- The timestamp always carries an explicit UTC offset. cron runs in **system local** time and the
  container runs in UTC; a bare local time would be unreadable six months later.
- **`exit` is authoritative for the verdict.** `steps` / `failed` are a best-effort count of the
  command's own marker lines and print as `?` when the runner cannot recognise the format. A
  monitor that admits it does not know beats one that confidently reports zero.
- `duration` is wall clock for the whole `docker compose exec`, so it includes container
  scheduling, not just the check.

Raw greps, when you would rather not use the reader:

```bash
grep '^LOGIN-CHECK' /var/log/findajob/login-check.log | tail -20
grep '^LOGIN-CHECK' /var/log/findajob/login-check.log | grep -c ' FAIL '
zgrep -h '^LOGIN-CHECK' /var/log/findajob/login-check.log* | sort -k2
```

### Rotation

Monthly, keeping 12 — one calendar year, which is the horizon people actually ask about. A
`maxsize 5M` guard rotates early if something starts spewing. `delaycompress` keeps the most
recent archive as plain text (it is the one you read while debugging a rotation). `create 0640
root adm` recreates the file immediately, because the writer is a plain `>>` redirection with no
long-lived file descriptor — which is also why `copytruncate` is deliberately **not** used.

---

## 6. Failure modes and what to do

Find the failing run's block first: `login-check-history --last`, or `--run <id>`.

### `FAIL` with `exit=124` — timed out

The check never returned within `LOGIN_CHECK_TIMEOUT` (180 s). This is a **hang**, not a reported
failure: a wedged socket, a container that is up but not scheduling, or DNS not resolving inside
the container. Go looking at the socket, not at Brevo.

```bash
ssh macbook-air "cd /root/findajob && docker compose ps"
ssh macbook-air "cd /root/findajob && docker compose logs --tail=200 api"
ssh macbook-air "cd /root/findajob && docker compose exec -T api ps -ef | grep check_login_flow"
```

Residual worth knowing: `timeout` kills the docker CLI on the **host**. A process blocked in a
syscall **inside** the container may survive it. It cannot block the next tick (the lock is
released either way), but repeated 124s plus orphaned processes means kill them, or
`docker compose restart api`.

### `FAIL` with `exit=127` — the runner could not start

`cannot cd to /root/findajob` means the project moved or the disk is not mounted;
`docker: not found` means `PATH` in the cron.d file was edited. Neither is an application fault.

### Lock contention — `flock: cannot acquire lock`

The previous run is still holding it. Given the `timeout`, that should be impossible; if you see
it, something is holding `/var/lock/findajob-login-check.lock` outside this system.

```bash
ssh macbook-air "fuser -v /var/lock/findajob-login-check.lock; ls -l /var/lock/findajob-login-check.lock"
```

Never "fix" this by deleting the lock file or dropping `-n` from the cron line. Overlapping runs
multiply the outbound mail at exactly the moment things are already going wrong.

### SMTP auth failure — `535`, `authentication failed`

Almost certainly the SMTP key. **It expires 2027-08-01.**

1. Brevo → Settings → SMTP & API → generate a new SMTP key (not an `xkeysib-…` API key).
2. Put it in `/root/findajob/.env` as `EMAIL_HOST_PASSWORD`.
3. `cd /root/findajob && docker compose up -d api` — **env is read at container CREATE**, so a
   bare `restart` keeps the old value and you will chase your tail.

Do **not** switch to the Brevo API backend to work around it: Brevo enforces an IP allowlist on
API calls and 401s this VPS (it egresses over IPv6). SMTP authenticates by credential instead,
which is exactly why it works here. Equally, do not click "Activate for SMTP keys" on Brevo's
IP-blocking banner.

### SMTP accepted but nothing arrives — the important one

The check will still say `PASS`, because acceptance is all it can see. Symptom is a user report,
not a red line.

1. Brevo → Transactional → Logs. Look for **`Delivered`**. An `Error` event sitting next to a
   `Sent` one is the signature of an unauthenticated sending domain.
2. Verify the DNS is still live and still ours:
   ```bash
   dig +short TXT mail.findajob.pro
   dig +short CNAME brevo1._domainkey.mail.findajob.pro
   dig +short TXT _dmarc.mail.findajob.pro
   ```
   Expected values are in the `findajob-deploy` skill. They are hand-maintained at Hostinger
   (NS `dns-parking.com`) and a panel edit elsewhere in the zone can clobber them.
3. Confirm `noreply@mail.findajob.pro` is still a verified sender in Brevo.
4. Check the address is not on `SuppressedAddress` — a hard bounce lands it there and the mailer
   then **skips silently** while still reporting success (that is intended: differing would make
   the suppression list an account-existence oracle).

### Django rejects the From address

`ValueError: period in 'phrase'` from Django 6's modern address parser. `DEFAULT_FROM_EMAIL`
needs inner double quotes, and because Compose's `env_file` parser also reads quotes, the whole
value must additionally be wrapped in single quotes:

```
DEFAULT_FROM_EMAIL='"Findjob.pro" <noreply@mail.findajob.pro>'
```

### No new lines in the log at all

The job is not running. In order:

```bash
ssh macbook-air "ls -l /etc/cron.d/findajob-login-check"   # must be root:root, 0644, no dot in the name
ssh macbook-air "systemctl status cron --no-pager"
ssh macbook-air "grep findajob /var/log/syslog | tail -20"
ssh macbook-air "ls -ld /var/log/findajob"                 # the redirection cannot create a directory
```

cron skips a cron.d file that is group- or other-writable, or whose filename contains a `.`, and
it says nothing about it. Also check the file ends with a newline — a final line without one is
ignored.

### The check itself is wrong

If a step is unreliable, fix the **command**, not the schedule. Nothing in `deploy/cron/` knows
what the steps are, and that is deliberate: the runner reproduces whatever the command prints, so
steps can be added, renamed or removed without touching any of these four files.

---

## 7. Deliberate non-goals

- **No alerting.** A failure lands in a log, not in an inbox — partly because the box has no MTA
  and no MX records for `findajob.pro`, and partly because an alerter that mails through the very
  service it monitors is worthless on the day it matters. If alerting is added later, it should
  go through a different channel than Brevo. `MAILTO=""` in the cron.d file suppresses cron's own
  delivery attempts for the same reason.
- **No `/healthz` HTTP endpoint.** A public route that provokes an OTP send is an abuse primitive
  and would need its own rate limit and auth. A management command on the box needs neither.
- **No metrics export.** There is no Prometheus on this VPS. The NDJSON-shaped `--json` output
  exists on the command if that changes.
- **No inbox verification.** Confirming actual receipt means an IMAP mailbox, credentials for it,
  and a second delivery dependency — and `findajob.pro` has no MX records at all today. That is a
  separate project, and until it exists, §2's boundary stands.
