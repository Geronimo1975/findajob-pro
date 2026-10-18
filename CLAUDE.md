# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

findajob.pro — a job-tracking / AI-assisted-application SaaS for the German market (EN default,
DE, RO). Two halves that deploy independently: a **static frontend** in `web/`, served by nginx,
and a **Django 6 + DRF API** in `backend/`, reachable at `/api/` on the same origin.

**Fresh start 2026-08-01.** The previous Django/Jobick platform was wiped from prod and local.
Anything mentioning Jobick, `dz.py`, `dashboard_views.py` or `.claude/rules/*` describes the dead
platform. `.claude/rules/` is empty on disk, but an agent may still be handed those files as
"project instructions" — ignore them.

## Commands

There is **no root-level entry point**: no Makefile, no package.json, no pyproject.toml. All
Python commands run from `backend/`. Two virtualenvs exist (`.venv/` and `backend/.venv/`), both
Python 3.12 with identical packages; either works, the working directory is what matters.

```bash
cd backend
../.venv/bin/pytest                                   # whole suite, ~3s
../.venv/bin/pytest tests/accounts/test_ratelimit.py  # one file
../.venv/bin/pytest tests/test_boot_checks.py::SharedCacheCheckTests::test_shared_cache_is_accepted
../.venv/bin/pytest -k subnet                         # by name
../.venv/bin/python manage.py runserver                # DEBUG on, OTP codes print to the console
../.venv/bin/python manage.py migrate                  # first local run: db.sqlite3 starts empty
../.venv/bin/python manage.py verify_platform   # external smoke of prod; --json, --module <name>
```

- **pytest must run from `backend/`.** `pytest.ini` lives there and supplies
  `DJANGO_SETTINGS_MODULE=config.settings.test`. From the repo root, collection dies with 20
  `ImproperlyConfigured` errors.
- **`manage.py test` alone is broken.** `manage.py` defaults to `config.settings.dev`; the test
  runner then forces `DEBUG=False` and a locmem email backend, which trips the boot checks
  (`findajob.E001`/`E006`) and runs nothing. Use
  `DJANGO_SETTINGS_MODULE=config.settings.test python manage.py test`. pytest is immune.
- **There is no linter or formatter anywhere** — no ruff/black/flake8/isort, no pre-commit, no CI.
  `requirements-dev.txt` is pytest + pytest-django and nothing else. Style is convention only.
- **Local DB is SQLite by omission.** `base.py` picks Postgres only when `POSTGRES_DB` is set;
  the repo-root `.env` does not set it, so local runs use `backend/db.sqlite3` + LocMemCache.
  Prod is Postgres 16 + Redis.
- Env loading: `backend/.env` then repo-root `.env`, both `override=False`, so the first file to
  define a key wins and the real process environment beats both.
- Management commands, by owning app (`ls backend/apps/*/management/commands/` for the live set):
  `issue_login_code --email <addr>` (accounts; break-glass — writes a real OTP row and prints the
  code, mails nothing), `seed_legal` (legal; see below), `seed_platforms` + `ingest_jobs` (jobs;
  the adapter registry's entry point), `check_login_flow` and `verify_platform` (common).
- `verify_platform` is an **external** smoke test of prod, not a unit test: it drives the real
  site over HTTPS. Its check modules live in `backend/apps/common/verify/` (`api_gate`,
  `auth_otp`, `frontend`, `outbox`, `automation`, `n8n`, `deliverability`, `gmail`), each
  returning `PASS`/`FAIL`/`SKIP` — a check that cannot run (missing credential, feature not
  deployed) must SKIP, never FAIL. `--module <name>` runs one.
- `web/` has no build, no tests and no linter. Deploy is `rsync -az --delete web/ …` with no
  restart. To preview locally you need an HTTP server (`python3 -m http.server` from inside
  `web/`) because the pages use ES modules, which `file://` blocks.

Deploy commands, VPS layout, nginx/Traefik specifics and the Brevo mail setup are all in the
**`findajob-deploy`** skill — read it rather than reconstructing them.

## Architecture

### The dashboard is server-rendered at `/app/` (2026-10-18 pivot)

A second frontend surface now exists alongside the static one: a **server-rendered
Django app `apps.dashboard`, mounted at `/app/`**, themed with the vendored Jobick
Bootstrap template (assets under `apps/dashboard/static/dashboard_app/`, all local —
no CDN). It is gated by the OTP **session** (the `session_required` decorator in
`apps/dashboard/auth.py` redirects anonymous visitors to `/auth.html?next=…`, which
now defaults post-login to `/app/`). This is a deliberate exception to the
"static frontend only" rule below — the **marketing** pages (`index.html`,
`unternehmen.html`, `auth.html`) stay static + nginx, but the **product dashboard**
is server-rendered. The old React prototype `app.html`/`support.js` is archived, not
extended. Design: `docs/superpowers/specs/2026-10-18-jobick-dashboard-design.md`;
later modules (profile, matching, chatbot) build on the pipeline-port spec
`docs/superpowers/specs/2026-09-26-pipeline-port-design.md`. Deploy note: a new
`apps/dashboard/static/` tree means `collectstatic` must run before nginx serves
`/app/`.

**Plan 2 (enrichment) shipped:** the dashboard defaults to a **dark Voltagent theme**
(`static/dashboard_app/css/theme-voltagent.css`, `data-theme="dark"` on `<html>`, a
persisted `#theme-toggle` to light; Inter + JetBrains Mono vendored under
`static/dashboard_app/fonts/`). The deterministic matcher is ported byte-identically
from `~/Projects/Job_app/filters/relevance.py` into `apps/jobs/matching.py`
(`prescore`, `skill_tokens`, `skill_match`). New app **`apps.profiles`**
(`UserProfile`, `SearchProfile.to_matching_config()`, lazy `get_or_create_*` in
`profiles/services.py`). The command center (`apps/dashboard/kpis.py` +
`index.html`) now shows a Documents summary, Recommendations, a **nomad-jobs table
with match %**, and a job-ingestion automation table — all owner-scoped
(`.filter(user=user)` is required because `OrgScopedQuerySet.for_user` scopes by org
membership, not owner). Deploy also needs `migrate` (profiles.0001).

### The gate is the API, not the shell

`index.html`, `app.html` and `auth.html` are served publicly by nginx and hold no user data, so
the demo stays reachable by anyone. Everything under `/api/v1/*` requires a session except
`auth/*`, `legal/*` and `i18n/*`. `app.html` boots by calling `GET /api/v1/me/`; on 401 it
redirects to `/auth.html?next=…`, `?demo=1` skips the call entirely, and an **unreachable** API
degrades to the demo fixtures rather than bouncing the visitor to a login that could not work.
A Traefik `forwardAuth` middleware is defined in `deploy/traefik/findajob.yml` and deliberately
attached to nothing.

### Login is a 6-digit email OTP, sent synchronously

Four routes, all under `/api/v1/auth/`: `csrf/`, `request-code/`, `verify-code/`, `logout/`.
There is no `/auth/me/` and no `/auth/session/` — the single session endpoint is `GET /api/v1/me/`,
owned by `apps.tenancy`. Several behaviours look like bugs until you read the spec:

- `request-code` is **branch-free**: identical 202 and identical timing whether or not the address
  has an account, which is why `EmailOTP` has no FK to `User`.
- **Up to 3 codes are live at once** and a resend does not supersede the older ones — German
  providers greylist and then deliver both, and the user types the older one.
- Mail send is **synchronous inside the request** with a 5s budget. There is no queue, so there is
  no plaintext code sitting on Redis and no "worker down = nobody can log in".
- On cache failure, `request-code` fails **open** (falls back to the DB for the cooldown) while
  `verify-code` fails **closed** on the subnet budget. A permanent 429 on login usually means the
  cache is unreachable, not that a limit was really hit.

### Tenancy: unscoped reads raise

Every tenant row hangs off one `Organization`. Org-scoped models use `OrgScopedManager`, which
raises `OrgScopeError` when a queryset is **evaluated** without scoping — `Membership.objects.all()`
raises; use `.for_org(org)`, `.for_user(user)`, or the greppable `.unscoped()` (admin, migrations,
management commands, tests). Because Django builds reverse managers from the default manager,
`org.memberships.all()` raises too. A request names its workspace with `?org=<id>` or
`X-FindaJob-Org`; with neither, the oldest membership wins.

`apps.accounts` owns exactly `User`, `EmailOTP` and `SuppressedAddress`. It must not import
`Organization`, `Membership` or `AuditLog` — the one permitted call into tenancy is
`bootstrap_workspace(user)`, imported lazily. That function creates one Organization and one
owner Membership and **nothing else**; a test reads its source with `inspect.getsource` and
asserts the words `Invitation`, `Subscription` and `UsageQuota` never appear, because
auto-accepting an invitation at login would be a cross-tenant account-capture hole.

### Jobs: platforms are adapters, and the risk table decides who gets one

`apps.jobs` owns `Platform` and `Job`, mounted at the API root (`/api/v1/platforms/`,
`/api/v1/jobs/`) because that is what `web/api.js`'s `ENDPOINTS` expects — not under a `jobs/`
prefix. Ingestion is the platform-adapter pattern: one isolated module per source under
`apps/jobs/adapters/`, each filling `BaseAdapter` and returning `NormalizedJob`, registered in
the `ADAPTERS` dict in `adapters/__init__.py`. **Never write per-platform logic into the core.**

Only LOW-risk feed/API sources get an adapter at all (today: RemoteOK, WeWorkRemotely). LinkedIn,
Indeed and Wellfound are semi-manual by the risk table and have **no registry entry on purpose** —
adding one is the hard rule below, not an oversight.

### Outbox: application mail leaves from the user's own mailbox

Application emails are sent through the user's `MailboxConnection` (SMTP today; OAuth Gmail/
Outlook specified but unbuilt), never from findajob infrastructure — sending "on behalf of" from
Brevo fails SPF/DKIM/DMARC, and the user's own mailbox also puts the message in their Sent folder
so replies arrive naturally. Brevo stays transactional-only (OTP mail). Auto-send is permitted
only where `apply_mechanism = email` and `auto_send = True`; the semi-manual platforms get
"copy & mark as sent" (`{"sent_manually": true}`) and dispatch nothing. Credentials are encrypted
via the app's `crypto.py`. Spec: `docs/2026-08-02-outbox-spec.md`.

Note the app is mid-relocation from `apps/outbox/` into `apps/tenancy/outbox/`; `INSTALLED_APPS`,
the `AppConfig.name` and `tests/outbox/` still say `apps.outbox`. Check the working tree before
assuming an import path.

### The admin is a first-class operator surface

`apps.common` is pinned **ahead of** the admin in `INSTALLED_APPS` (order is load-bearing —
`APP_DIRS` template lookup returns the first match, and `apps.common` ships overrides). The admin
is a custom `AdminSite` (`apps/common/admin_site.py`) that overrides `get_app_list` to order the
index along the path an operator actually walks — never cheat the sort by prefixing verbose names,
which corrupts every label downstream. `apps.automation` is a **read-only** n8n panel mounted at
`admin/automation/` and wrapped in `admin.site.admin_view`; it must stay listed **before**
`admin.site.urls`, whose catch-all would otherwise swallow it. It claims `index_template` only if
still unset — the hand-off point between the two owners is the `admin/_n8n_panel.html` include.
The n8n instance is the **personal** account (app.n8n-studio.com), and its API key must never be
logged (`apps.common.logging.RedactingFilter` is the backstop).

### One error shape

Every failure is `{"error": {"code": "<machine_code>", "detail": "<human>"}}`, produced by
`apps/common/errors.py` (DRF exception handler + the CSRF failure view, wired in settings).
`web/api.js` only knows how to read that shape. DRF defaults to cookie-session auth and
`IsAuthenticated`, JSON only — a new view is protected unless its author opts out.

### The frontend is a rendered prototype, not hand-written HTML

`web/*.html` are `.dc.html` documents exported from a visual editor: a `<x-dc>` template with
custom tags plus an inline script, rendered by `support.js` — a **generated** React runtime with
no build in this repo. Consequences: React and both fonts are self-hosted and injected through a
`window.__resources` URL map set **before** `support.js`; styling is 100% inline, so responsive
rules live in each page's `<style>` block and need `!important` plus attribute selectors
(`[style*="repeat(3, 1fr)"]`); the CSP has to allow `unsafe-eval` because the runtime evaluates
its page script through `new Function`.

`web/api.js` has two jobs: the **single owner of HTTP** (one `request()`, CSRF, `ApiError`,
`bootstrapCsrf`) and the demo fixture store. Do not add a second fetch helper. Its `ENDPOINTS`
map is still partly aspirational — auth, `/me/`, `/platforms/`, `/jobs/` and the outbox routes
exist server-side; the rest do not, and most screens remain fixture-driven. Check
`config/urls.py` before assuming an endpoint is real.

### legal.js is the single source for legal text

`web/legal.js` (6 documents × EN/DE/RO) is both what the browser renders and what
`manage.py seed_legal` hashes into `LegalDocument`, parsed by a hand-written JS-object reader
because there is no Node in the image. Consent is resolved **server-side** at verify time and
fails closed with 503 if unseeded, so an unseeded database means nobody can complete a first
login. Re-run `seed_legal` after every `legal.js` change. In Docker it only works because
compose mounts `./web:/web:ro` into the api container.

## Sources of truth, in precedence order

1. `docs/2026-08-01-auth-implementation-spec.md` — self-declared AUTHORITATIVE; code docstrings
   cite it by section ("spec §5"). `docs/2026-08-01-database-schema.md` records the live schema,
   where the database wins over the models. Narrower specs govern their own subsystem:
   `docs/2026-08-02-outbox-spec.md`, `docs/2026-08-02-login-healthcheck.md`, and
   `docs/superpowers/specs/2026-08-02-verify-platform-design.md`.
2. `main_prompt.md` — the product architecture (platform-adapter pattern, per-platform risk,
   MVP list). Written in Romanian. Its stack section says Django 5.x and is **superseded**: the
   decision is Django 6.0, pinned `>=6.0,<6.2`.
3. `web/` — what actually ships. `prototype/` is design intent only; the two have diverged
   (`web/api.js` carries the whole HTTP layer, `web/legal.js` the real Impressum data).
   Within `prototype/`, the 13-screen file is `Job Tracking Platform Voltagent.dc.html`;
   `Job Tracking Platform.dc.html` is a superseded 7-screen generation and
   `design_handoff_findjob_pro/` is a stale snapshot — read neither.

## Invoke these skills before working

- backend / models / modules / product architecture → **`findajob-architecture`** (adapter
  contract, per-platform risk table, data model, audited API-contract gaps)
- any UI → **`voltagent-design`** (tokens, the 13 screens, component chrome)
- deploy / infra / DNS / TLS / "site is down" → **`findajob-deploy`**

## Hard rules

- **UI-first.** findajob.pro must serve the interface at every point of any change; the backend
  wires in underneath. This is why the API gate degrades instead of redirecting.
- **Never automate submission on LinkedIn or Indeed** — account ban and legal risk respectively.
  Semi-manual flows only, per `main_prompt.md` §10.
- **The frontend contacts no third-party origin, with exactly one consent-gated exception.**
  React and fonts are self-hosted under `web/vendor/` and `web/fonts/`; nginx sends
  `default-src 'self'`. A Germany-targeted site must not ship a visitor's IP to a US CDN. Never
  re-introduce a CDN tag. The single exception is GA4, owned solely by `web/analytics.js`, which
  touches the network **only after** the visitor clicks "Accept all" — § 25 (1) TTDSG requires
  consent *before* access to the device. Never paste the raw `gtag.js` snippet into any `.html`:
  a `<script async src>` in markup fires on load, before the banner can be answered, which is the
  exact violation the module exists to prevent. `web/consent-banner.js` is the banner for
  `index.html` and `auth.html`; `app.html` has its own inside the `.dc.html` template and writes
  the same localStorage key. Changing the vendor or measurement ID means editing `web/legal.js`
  and re-running `seed_legal`.
- **The published Impressum carries a real person's real address** (§ 5 DDG requires it, and it
  was supplied deliberately). Never replace it with placeholder or invented company data, and
  never invent an HRB or VAT number.
- `documents/` holds the operator's real CV and signature. `.gitignore` keeps the directory
  skeleton and never the contents — never `git add -f` there, never quote it.
- No heavy new dependencies without asking.
