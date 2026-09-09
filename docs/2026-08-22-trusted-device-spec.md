# Trusted devices — specification

**Status:** authoritative for `apps.devices`.
**Date:** 2026-08-22.
**Relationship to `docs/2026-08-01-auth-implementation-spec.md`:** additive. That document
remains authoritative for everything it covers; this one records two deliberate deviations
from it (§7 below) and owns everything under `apps.devices`.

## 1. Why this exists

Login is a 6-digit email OTP sent synchronously (auth spec §1). That is the right default, but
a job seeker applying actively signs in repeatedly — checking application status, generating
documents — and a full email round trip every time is real friction. German providers greylist,
which makes that round trip slow exactly when the user is least patient.

A trusted device lets a returning user skip the email round trip on a machine they have
explicitly marked, while remaining individually revocable.

**What this is not.** A trusted-device cookie that mints a session is, in raw strength, equivalent
to a long-lived session cookie: whoever holds it holds the account. It is not a second factor and
must never be described as one. Its advantages over simply raising `SESSION_COOKIE_AGE` are that
it is *enumerable* and *revocable per device*, which is what makes the self-service device list
possible.

## 2. Model — `devices.TrustedDevice`

| Field | Notes |
|---|---|
| `user` | FK to `AUTH_USER_MODEL`, `CASCADE` |
| `device_id` | `UUIDField`, unique, indexed — the public half, safe to log |
| `secret_hash` | `CharField(64)` — HMAC-SHA256 of the secret, keyed by `SECRET_KEY` |
| `expires_at` | 30 days from issue |
| `last_used_at` | nullable; updated on every successful device login |
| `revoked_at` | nullable; set by explicit revocation, never by logout |
| `user_agent`, `ip`, `label` | display only, for the device list |
| `created_at` | `auto_now_add` |

**The plaintext secret is never stored**, exactly as with `EmailOTP.code_hash`. It exists in one
process for one request: generated, hashed, written as a hash, set as a cookie, dropped. The
HMAC key salt mixes in the `device_id`, so a hash captured for one device cannot be replayed
against another.

A device is *live* when it is un-revoked, un-expired, and its user is still active.

## 3. Cookie

Name `findajob_device`. `HttpOnly`, `SameSite=Lax`, `Secure` mirroring `SESSION_COOKIE_SECURE`,
`Max-Age` 30 days, `Path=/`.

Value is `{device_id}.{secret}` — the id selects the row with one indexed lookup, the secret is
compared with `constant_time_compare`. Splitting them this way means a wrong secret costs one
query and one HMAC, not a table scan.

## 4. Endpoint — `POST /api/v1/auth/device-login/`

Request `{email}`. Response on success is `200` with the same payload as `GET /api/v1/me/`, so
the frontend redirects into the app exactly as it does after `verify-code`.

Rejection is always `401 device_not_trusted`, with no distinction between: no cookie, malformed
cookie, unknown `device_id`, wrong secret, expired, revoked, inactive user, or an email that does
not match the device's owner. The client's only correct response to any of them is to fall back
to the normal OTP flow, so a finer taxonomy would leak without buying anything.

The email is required and must match the device's owner. It does not add cryptographic strength —
whoever holds the cookie could read the address off the app anyway — but it keeps a deliberate
human action in the flow, which is what the user chose over silent auto-login.

## 5. Rate limiting

A **dedicated** budget: 10 failures / 15 min per `(email, /24)`, fail-closed, keyed
`otp:dl:{email}:{subnet}`.

Deliberately *not* the verify-failure budget. Sharing it would let an attacker burn a victim's
OTP verify budget by posting junk device cookies — a denial-of-service against the legitimate
login path. The two budgets protect different credentials and must be independent.

## 6. Trusting a device

`POST /api/v1/auth/verify-code/` accepts an additional optional `remember_device: bool`,
defaulting to `false`. When true and the verify succeeds, one `TrustedDevice` is minted and the
cookie is set on the response.

Opt-in, never automatic: marking a library or internet-café machine as trusted without being
asked is exactly the failure this checkbox exists to prevent.

**Logout does not revoke the device.** `POST /auth/logout/` ends the session and nothing else;
revoking a device is a separate explicit action. Revoking on logout would make the feature
pointless, since logging out is precisely when a user next needs the shortcut.

## 7. Deviations from the auth implementation spec

1. **A fifth route under `/api/v1/auth/`.** Auth spec §3 pins exactly four. The alternative was
   to let `request-code` sometimes return a session — rejected, because `request-code` being
   **branch-free** (auth spec §6) is the most security-critical property in the module, and
   giving it a second responsibility would entangle the thing most worth keeping simple. The
   pinned inventory tests are updated to five and cite this document.

2. **A new rate-limit budget** in `apps.accounts.ratelimit`, per §5 above.

Everything else in the auth spec — the branch-free `request-code`, the three live codes, the
five-attempt row burn, fail-open/fail-closed asymmetry, the single `GET /api/v1/me/` session
endpoint — is unchanged.

## 8. Audit

Via `apps.tenancy.services.record_security_event`, the same bridge login already uses:

| Action | When |
|---|---|
| `auth.device_login_succeeded` | a device login minted a session |
| `auth.device_login_failed` | any rejection, with a `reason` in the payload |
| `auth.device_trusted` | a device was minted at verify time |

## 9. Out of scope here

Listing and revoking devices, and the new-device email alert, are the next piece of work. This
document covers minting, using and expiring a device only.
