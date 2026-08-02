# Outbox — send-as-user email (spec, 2026-08-02)

Status: **Phase 1 implemented** (mailbox connection, SMTP provider).
Phase 2 (OAuth Gmail/Outlook) and Phase 3 (`applications/<id>/send/`) are
specified here but blocked on external OAuth credentials and on the `jobs`
app respectively.

## Principle

Application emails (personalized email + cover letter + attached documents)
are sent **from the user's own mailbox**, never from findajob infrastructure.
Sending "on behalf of" a user from Brevo would fail SPF/DKIM/DMARC and land in
spam; sending through the user's mailbox also puts the message in their Sent
folder so recruiter replies arrive naturally. Brevo remains transactional-only
(OTP mail).

Auto-send is allowed **only** on platforms with `apply_mechanism = email` and
`auto_send = True` (the LOW-risk RSS boards). LinkedIn / Indeed / Wellfound
never send anything: "Copy & mark as sent" only (`{"sent_manually": true}`).

## Data model — `apps.outbox`

Not in `apps.accounts` (which is pinned to User/EmailOTP/SuppressedAddress).

### MailboxConnection

One mailbox per member per organization (MVP). Org-scoped via
`OrgScopedManager(user_field="user")`.

| field | notes |
|---|---|
| `org`, `user` | unique together (`one_mailbox_per_member`) |
| `provider` | `smtp` (live) / `gmail` / `outlook` (phase 2) |
| `email_address`, `display_name` | the real From |
| `credentials_encrypted` | Fernet blob. smtp: `{host, port, username, password, use_tls}`; oauth: `{refresh_token}` |
| `status` | `connected` / `error` / `revoked` |
| `last_verified_at`, `last_error_code` | e.g. `auth_failed`, `smtp_timeout` |

Secrets never leave the serializer — the API returns provider, address,
display name, status only (same rule as `Integration.key_hint`).

Encryption key: `OUTBOX_FERNET_KEY` env var (urlsafe-base64, 32 bytes). When
unset (dev/test) a key is derived from `SECRET_KEY`; prod must set it
explicitly so key rotation is independent of the Django secret.

### EmailSendLog (phase 3, with the jobs app)

`org`, `application FK (PROTECT)`, `mailbox FK (SET_NULL)`, `to_address`,
`subject`, `body_snapshot` (exactly what left, post-Review edits),
`attachment_names JSON`, `provider_message_id`, `status queued/sent/failed`,
`error JSON`, `created_at/sent_at`. PROTECT: an application with proof of send
is only deleted through an explicit GDPR flow. `body_snapshot` is personal
data → included in future GDPR export/erasure.

### Extensions elsewhere (when jobs lands)

- `Application.sent_via = FK(MailboxConnection, null=True)`.
- `JobListing.platform_specific_data.apply_email` required at ingest for
  `apply_mechanism="email"` platforms; without it the job cannot enter the
  auto-send flow.
- Daily caps: `Platform.rate_limit_config` + `UsageQuota.apps`.

## Endpoints — `/api/v1/outbox/`, session + CSRF, canonical error envelope

| method | route | behaviour |
|---|---|---|
| GET | `mailbox/` | current member's mailbox in current org; 404 `mailbox_not_connected` |
| POST | `mailbox/` | connect SMTP; server verifies (SMTP login, sends nothing) before saving; failure → 400 `smtp_verification_failed`, nothing stored |
| POST | `mailbox/test/` | send a test mail to the user's own address; 202 / 502 `send_failed` |
| DELETE | `mailbox/` | revoke + delete secrets; 204 |
| GET | `mailbox/oauth/start/?provider=` | phase 2 — `{authorize_url}`, state bound to session **and** org (cross-tenant guard) |
| GET | `mailbox/oauth/callback/` | phase 2 |

POST body (smtp):

```json
{"provider": "smtp", "email_address": "ion@gmx.de", "display_name": "Ion Popescu",
 "smtp": {"host": "mail.gmx.net", "port": 587, "username": "ion@gmx.de",
          "password": "app-password", "use_tls": true}}
```

GET response (never secrets):

```json
{"provider": "smtp", "email_address": "ion@gmx.de", "display_name": "Ion Popescu",
 "status": "connected", "last_verified_at": "2026-08-02T10:00:00Z",
 "last_error_code": null}
```

## Phase 3 — `POST /api/v1/applications/<id>/send/`

Accepts the **edited Review payload** (fixes the audited contract bug where
drawer edits were discarded): `subject`, `body`, `cover_letter`,
`document_ids`, `notes`, `sent_manually`.

Validation order and codes:

1. 409 `wrong_status` — application not in REVIEW.
2. 409 `auto_send_forbidden` — platform not `auto_send`; only
   `{"sent_manually": true}` accepted there.
3. 409 `mailbox_not_connected`.
4. 422 `missing_apply_email`.
5. 429 `daily_send_limit`.

Success: persist edits, create `EmailSendLog(queued)`, enqueue the send task
(JSON-safe args: IDs only), return 202 with `send_log_id`. Task builds the
MIME message, decrypts credentials in memory only, sends, sets
`Application.SENT` + `sent_at` or `FAILED` + log error. Companion endpoints:
`GET applications/<id>/send-status/` (UI poll) and
`POST applications/<id>/send/retry/`.
