# M1 — auth-identity + core-tenancy + legal-consent · implementation spec

**Status:** decided 2026-08-01. This document is AUTHORITATIVE. Where any earlier design note,
agent output or prototype file disagrees, this wins. It exists because the adversarial review
found that independently-written designs contradicted each other on CSRF, on the session
endpoint, and on module ownership — so every contract below is pinned exactly once, here.

Stack: **Django 6.0** (pin `Django>=6.0,<6.2`) + DRF · PostgreSQL 16 · Redis (cache only) ·
static frontend served by nginx, same-origin `/api/` proxy · Traefik TLS.

---

## 1. Decisions taken (do not re-litigate)

| Decision | Value | Why |
|---|---|---|
| Mechanism | 6-digit email OTP, hand-rolled | No new Python dep; cross-device works; magic links break on corporate link-scanners and land the session on the wrong device |
| Session | Django session cookie, `HttpOnly; Secure; SameSite=Lax` | Frontend and API are same-origin; JWT-in-localStorage is XSS-exfiltratable and buys nothing |
| Send path | **Synchronous**, inside the request, 5 s timeout | Kills three blockers at once: no plaintext code on a Redis queue, no "worker down = nobody logs in", no green-UI-zero-delivery |
| Sender | Brevo via `django-anymail` | Already working on this VPS; EU company, DPA available; swapping to Mailgun EU is a settings change |
| DNS / mailbox | Hostinger | Registrar + DNS (`dns-parking.com` NS). Mailbox is for RECEIVING only — never the OTP sender |
| App gate | **API-level, in Django** | See §7 — the demo requirement forces the shell to stay public, so the data gate must be the API |
| Apps created | `apps/tenancy`, `apps/accounts`, `apps/legal` | Build order positions 1, 3, 4. Nothing from positions 5+ may be imported |

---

## 2. Module boundaries — hard

`apps/accounts` owns **exactly two** models: `User`, `EmailOTP`. Plus `SuppressedAddress`.

It must **not** define, import or instantiate: `Organization`, `Membership`, `AuditLog`
(those are `apps/tenancy`), `Invitation` (that is `org-members`, position 7 — not built),
`Subscription`, `UsageQuota` (positions 5–6 — not built).

`bootstrap_workspace()` lives in `apps/tenancy.services` and creates **only**
`Organization` + `Membership(role=owner)`. It **never** consults `Invitation` — invitation
acceptance is a separate, token-authenticated endpoint shipped with `org-members`, because
auto-accepting on login is a cross-tenant account-capture hole.

`AUTH_USER_MODEL = "accounts.User"` is set **before the first migrate**. There is exactly one
chance at this.

---

## 3. Endpoints — the complete, exclusive list

| Method | Path | Auth | Body / notes |
|---|---|---|---|
| GET | `/api/v1/auth/csrf/` | anon | Sets the `csrftoken` cookie. Returns `{"detail":"ok"}` |
| POST | `/api/v1/auth/request-code/` | anon, `@csrf_protect` | `{email, lang}` → **always** `202 {masked_email, expires_in, resend_in}` |
| POST | `/api/v1/auth/verify-code/` | anon, `@csrf_protect` | `{email, code, accepts_terms}` → `200` + session |
| POST | `/api/v1/auth/logout/` | session | → `204` |
| GET | `/api/v1/me/` | session | **Owned by `apps/tenancy`** |

**There is no `/auth/me/` and no `/auth/session/`.** The single session endpoint is
`GET /api/v1/me/`, shape:

```json
{"user":{"id":1,"email":"a@b.de","is_verified":true,"lang":"de"},
 "org":{"id":1,"name":"…","slug":"…"},
 "membership_role":"owner","memberships":[…],"lang":"de"}
```

`usage` and `flags` are added later by `entitlements-quotas` — additive only, so the shell
never breaks.

Error envelope for every failure: `{"error":{"code":"<machine_code>","detail":"<human>"}}`.
Codes used here: `invalid_email`, `rate_limited`, `invalid_code`, `code_expired`,
`too_many_attempts`, `terms_required`, `terms_outdated`, `delivery_unavailable`.

---

## 4. `EmailOTP`

```
email            EmailField(db_index=True)      # lowercased, NFKC-normalised
code_hash        CharField(64)                  # salted_hmac("findajob.otp", code).hexdigest()
purpose          CharField  = "login"
created_at       DateTimeField(auto_now_add)
expires_at       DateTimeField                  # created_at + 10 min
attempts         PositiveSmallIntegerField = 0  # max 5, then the ROW dies (not the account)
consumed_at      DateTimeField(null=True)
request_ip       GenericIPAddressField(null=True)
```

Constraint: `CheckConstraint(condition=Q(attempts__lte=5), name="otp_attempts_bounded")`
— note `condition=`, not `check=` (removed in Django 6.0).
Index: `Index(fields=["email", "-created_at"])`.

**Rules**

- Code = `f"{secrets.randbelow(10**6):06d}"`. Never stored, logged, or put in an audit payload.
- Hash with `django.utils.crypto.salted_hmac` — **not** PBKDF2 (a 6-digit space makes a slow
  hash pointless, and it would add ~300 ms to every verify).
- **Up to 3 live codes per email may coexist.** Do NOT supersede previous codes on resend:
  German providers greylist, then deliver both codes seconds apart, and the user types the
  older one. Verify iterates live rows newest-first with `constant_time_compare`.
- Single-use burn under `select_for_update(of=("self",))` — never `select_for_update()` bare
  next to a nullable join.
- Verify compares in constant time and increments `attempts` on failure.

---

## 5. Rate limiting — no `cache.ttl()`

`cache.ttl()` **does not exist** in Django's cache API (it is `django-redis`, which we do not
add). Store the deadline explicitly:

```python
cache.add(key, 0, window)
cache.set(key + ":exp", time.time() + window, window)
wait = max(1, int(cache.get(key + ":exp", 0) - time.time())) or window
```

| Budget | Limit | Key |
|---|---|---|
| Resend cooldown | 60 s | `otp:cd:{email}` |
| Per email | 5 / hour | `otp:req:{email}` |
| Per IP | 20 / hour | `otp:ip:{ip}` |
| Global | 500 / hour | `otp:global` |
| Verify failures | per-(email, /24 subnet), 10 / 15 min | `otp:vf:{email}:{subnet}` |

**Lock the credential, not the identity.** There is no per-email account lockout: 30 wrong
guesses from an attacker must never deny login to the mailbox owner. The per-row
`attempts<=5` burn is the real control — it forces the attacker back to the inbox for a new code.

**Redis failure = degrade, not 500.** Wrap every cache call. On `request-code`, fail OPEN and
fall back to the DB for the cooldown (`EmailOTP.objects.filter(email=…,
created_at__gt=now()-60s).exists()` — a row we write anyway). On `verify-code`, fail CLOSED
only for the subnet budget. A login outage is worse than a temporarily loosened limit.

---

## 6. Enumeration, bounces, break-glass

- `request-code` is **branch-free**: identical `202` and identical timing whether or not the
  address has an account. Never reveal existence.
- Because of that it will mail non-existent addresses. Therefore `SuppressedAddress(email,
  reason, created_at)` exists from day one: hard bounces and complaints land there via the
  Brevo webhook (`POST /webhooks/email/brevo/`, signature-verified), and `issue_login_code`
  skips suppressed addresses while still returning `202`.
- **Break-glass, shipped in the same commit as `0001_initial`:**
  `python manage.py issue_login_code --email X` writes a real `EmailOTP` row and prints the
  code to stdout, mailing nothing. Without it, a sending-domain suspension locks out
  everyone including the founder.
- **Boot-time check** (a `django.core.checks` deploy check, not a comment): refuse to boot when
  `not DEBUG` and the email backend is console, or the Anymail key is empty. An empty mail
  config must never produce a green UI and zero delivered codes.

---

## 7. The gate — and why it is not Traefik forwardAuth

The two decisions "gate at Traefik" and "keep the public demo" pull against each other:
forwardAuth gates the **shell**, but the demo requires the shell to stay reachable by anyone.
Resolution:

- `app.html` and `index.html` stay **publicly served by nginx**. They contain no user data —
  only the shell and the demo fixtures.
- **The gate is the API.** Every `/api/v1/*` route requires an authenticated session except
  `auth/*`, `legal/*` and `i18n/*`. This is where the data actually lives, so this is where the
  gate belongs.
- Boot: `app.html` calls `GET /api/v1/me/`; on `401` it redirects to `/auth.html?next=…`.
  With `?demo=1` it skips the call entirely and renders the existing `api.js` fixtures.
- The Traefik `forwardAuth` middleware is still **defined** in
  `deploy/traefik/findajob.yml` but attached to nothing yet; it becomes useful the day we
  gate an admin surface.

---

## 8. CSRF — one owner, one implementation

`web/api.js` is the **single** owner of HTTP. There is no `web/auth.js` with a second helper.

```js
export const ENDPOINTS = {
  authCsrf:    '/auth/csrf/',
  authRequest: '/auth/request-code/',
  authVerify:  '/auth/verify-code/',
  authLogout:  '/auth/logout/',
  me:          '/me/',
  // …existing entries unchanged
};

async function request(path, {method = 'GET', body} = {}) {
  const res = await fetch(BASE + path, {
    method,
    credentials: 'same-origin',                 // NOT 'include' — same-origin
    headers: {
      ...(body ? {'Content-Type': 'application/json'} : {}),
      ...(method !== 'GET' ? {'X-CSRFToken': readCookie('csrftoken')} : {}),
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  …
}
```

`bootstrapCsrf()` is exported and called on load of **both** `index.html` and `auth.html`
before any form can submit. DRF's `APIView.as_view()` is `csrf_exempt`, so both anonymous POST
views carry `@method_decorator(csrf_protect, name="dispatch")` explicitly — without it, login
CSRF is live.

---

## 9. Mocks must fail CLOSED

```js
export const USE_MOCK = window.__FINDJOB_MOCK__ === true;
if (USE_MOCK && !/^(localhost|127\.|.*\.local)$/.test(location.hostname)) {
  throw new Error('mock API blocked off-localhost');
}
```

Live is the default; mocks are opt-in. `demoLogin()` and any constant demo code are **deleted
from the shipped bundle** — the demo renders fixtures without minting a session. Demo mode
never produces a `sessionid` cookie.

---

## 10. Consent — server-resolved, never client-supplied

`apps/legal` ships `LegalDocument` (doc_id, lang, version, content_hash, effective_from,
is_current) seeded from `web/legal.js`, and `ConsentRecord` (append-only: user, type, granted,
doc_version, content_hash, ip, user_agent, ui_language, created_at).

At verify time, inside the same transaction that creates the user:

- resolve `LegalDocument.objects.get(doc_id="terms", lang="de", is_current=True)` **on the
  server** — the DE version is the legally binding one and its hash is the only defensible
  record. A client-sent version string is used **only** to detect staleness → `409
  terms_outdated`.
- write `ConsentRecord` rows for `terms` and `privacy`.
- the verify form carries an **affirmative checkbox**, not passive grey text. `accepts_terms`
  false on first-ever login → `400 terms_required`.

`send_on_behalf` consent is a separate, separately revocable record — the send pipeline
hard-blocks without it. Not collected at login.

---

## 11. Frontend flow — `web/auth.html`

Two steps in one page, Voltagent dark theme (`voltagent-design` skill is binding: canvas
`#101010`, hairline `#3d3a39`, accent `#00d992` for the primary button only, Inter + JetBrains
Mono, radius 6 px on buttons, 44 px touch targets).

1. **Email** → one field, one primary button.
2. **Code** → six-digit input accepting paste, a resend countdown from `resend_in`, an
   affirmative terms checkbox on first login, and a visible "wrong address?" path back to step 1.

**Pending state must survive a tab switch.** On a successful request, write
`{email, step:'code', sentAt, expiresAt}` to `sessionStorage` *and* `localStorage`, and
rehydrate on mount. The mainline mobile path is: type email → switch to the Mail app → come
back. iOS Safari evicts backgrounded tabs; without rehydration the user returns to an empty
form and every code they were sent is orphaned. `legal.js` already classifies login/session
storage as strictly necessary under §25(2) TTDSG, so this needs no consent banner.

Strings live in EN / DE / RO in the existing `T` table structure, added in all three at once.

---

## 12. Definition of done

- `manage.py check --deploy` clean; the boot-time mail check fires when misconfigured.
- Tests: single-use burn, expiry, `attempts` cap, three-live-codes, constant-time compare,
  enumeration parity (identical status **and** shape for known vs unknown address), CSRF
  required on both POSTs, rate-limit returns `429` with `retry_after` (never `AttributeError`),
  Redis-down degrades instead of 500, no plaintext code anywhere in logs or DB,
  `bootstrap_workspace` never touches Invitation, consent hash resolved server-side,
  demo mode mints no session, mock blocked off-localhost.
- `findajob.pro` still serves the existing UI throughout — UI-first is not negotiable.
