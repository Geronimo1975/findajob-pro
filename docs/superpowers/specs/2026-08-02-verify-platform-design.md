# verify_platform — external smoke verification of findajob.pro (design, 2026-08-02)

## Goal

One command that verifies, **from the outside, as a real visitor**, that every
module of findajob.pro works for external users — frontend, API, the full OTP
login flow, outbox send-as-user mail, automation endpoints, and the n8n
workflows — including proof that the mails land in **Inbox, not Spam**.

## Form

Django management command:

```
../.venv/bin/python manage.py verify_platform [--target https://findajob.pro]
                                              [--module <name>] [--json]
```

- Lives at `apps/common/management/commands/verify_platform.py`, with one
  check class per module under `apps/common/verify/` so each module is
  testable and readable in isolation.
- Default target is `https://findajob.pro`; `--target http://localhost:8000`
  works against a local runserver.
- Human output: one section per module, each check PASS / FAIL / SKIP with a
  one-line reason. `--json` emits the same as a machine-readable report.
- Exit code is non-zero iff any check FAILed. SKIP never fails the run.
- Cron-able on the VPS later with zero changes.

## Modules and checks

### `frontend`
- `GET /`, `/app.html`, `/auth.html` → 200, HTML content type.
- Every URL in the page's `window.__resources` map (React, fonts) → 200.
- Response carries CSP with `default-src 'self'`.
- No third-party origin appears in any `src=`/`href=` of the served HTML
  (hard rule: the frontend contacts no third-party origin).
- `web/legal.js` and `web/api.js` load (200, non-empty).

### `api-gate`
- `GET /api/v1/me/` unauthenticated → 401 with the canonical error shape
  `{"error": {"code": ..., "detail": ...}}`.
- `GET /api/v1/auth/csrf/` → sets the CSRF cookie.
- A 404 under `/api/v1/` also returns the canonical error shape.

### `auth-otp` (full real login)
- csrf → `POST auth/request-code/` for the test account
  (`VERIFY_TEST_EMAIL`, default `george@rotocon.world`) → 202.
- Poll Gmail over IMAP for the OTP mail (subject + received-after filter),
  extract the 6-digit code.
- `POST auth/verify-code/` → session established; `GET /api/v1/me/` → 200
  and echoes the test address; `POST auth/logout/` → session gone
  (`/me/` back to 401).
- The authenticated session is kept in-process and reused by the `outbox`
  and `automation` modules before logout.

### `mail-deliverability`
- The OTP mail from `auth-otp` was found in **INBOX**, not `[Gmail]/Spam`.
- Its `Authentication-Results` header shows `spf=pass`, `dkim=pass`,
  `dmarc=pass`.
- DNS: findajob.pro publishes an SPF TXT record, a DMARC record
  (`_dmarc.findajob.pro`), and the Brevo DKIM selector resolves. DNS lookups
  shell out to `dig` (no new dependency).

### `outbox`
- Authenticated: `GET` the mailbox-connection endpoint → 200 with the
  non-secret shape (provider, address, status; never credentials).
- If a `MailboxConnection` with status `connected` exists: trigger a test
  send through the user's own SMTP, then confirm via IMAP that it arrived
  in Inbox (not Spam). If none exists → SKIP with instructions.
- Endpoint absent on the target (not yet deployed) → SKIP "not deployed".

### `automation`
- Authenticated: the `apps.automation` endpoints respond 200 with valid
  JSON; unauthenticated they return 401 in the canonical error shape.
- Absent on the target → SKIP "not deployed".

### `n8n`
- Via the personal n8n instance's REST API (`N8N_VERIFY_URL`,
  `N8N_VERIFY_API_KEY` env): findajob-related workflows are active, their
  most recent executions did not error, and any findajob webhook answers a
  ping. Credentials unset → SKIP.

## Gmail access

IMAP (`imap.gmail.com`) with an app password, read from env:
`VERIFY_GMAIL_USER`, `VERIFY_GMAIL_APP_PASSWORD`. This keeps the command
fully autonomous (cron-friendly) and independent of any interactive Gmail
integration. When unset, `auth-otp` falls back to SKIP after the 202 (it
cannot fetch the code) and `mail-deliverability` SKIPs; nothing FAILs for
missing credentials — only for real breakage.

Mail matching is by subject prefix plus a received-after timestamp taken just
before `request-code`, so stale mails from earlier runs are never matched.

## Degradation rules

- Not-yet-deployed API modules → SKIP, never FAIL. The command is useful
  today (frontend + auth only on prod) and after the jobs/outbox deploy.
- Missing env credentials → SKIP with a one-line "how to enable" hint.
- Network timeouts against the target → FAIL (that is exactly an outage a
  visitor would see). Per-request timeout 15s; OTP mail wait up to 90s.

## Dependencies

`requests` (already pinned in `requirements.txt`), stdlib `imaplib`/`email`,
`dig` via subprocess. No new dependencies at all.

## Testing

Unit tests in `tests/common/test_verify_platform.py` with mocked HTTP/IMAP:
report aggregation, exit codes, SKIP-vs-FAIL rules, OTP extraction, and the
Authentication-Results parser. No live network in the test suite.
