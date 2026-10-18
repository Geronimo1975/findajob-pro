# Jobick Dashboard Shell + OTP Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a server-rendered, Jobick-themed dashboard served by the findajob
backend at `/app/`, gated by the existing OTP session, whose command-center index
shows real org-scoped data (jobs, applications) — the foundation the later plans
build modules onto.

**Architecture:** A new server-rendered Django app `apps.dashboard` (plain Django
views + templates, **no DRF**) mounted at `/app/`. The Jobick Bootstrap theme is
vendored as static assets and split into a `base.html` layout (sidebar + topbar)
that module templates extend. A `session_required` decorator reuses the Django
auth session that OTP verify already establishes; session-less visitors are
redirected to the existing static `auth.html?next=<url>`. Org scoping reuses the
tenancy resolution rule (oldest membership when unspecified).

**Tech Stack:** Django 6.0, Django template language, Bootstrap 5 (Jobick theme),
pytest + pytest-django (run from `backend/`).

**Spec:** `docs/superpowers/specs/2026-10-18-jobick-dashboard-design.md`

## Plan sequence (this plan is #1 of 5)

1. **Dashboard shell + OTP gate** (this plan) — layout, gate, command-center index on real data.
2. Candidate Profile & Preferences (`apps.profiles`) + Profile/CV screen.
3. Job matching + Recommendations view (profile → JobListing score).
4. Applications module (list, status flow, send via outbox / copy-&-mark-sent) + Documents module.
5. Chatbot assistant (backend Claude API, persisted history, daily cost cap) + real-history import command.

Each later plan is written after the prior ships. This plan produces a working,
navigable, honest dashboard on its own.

**Plans 2–5 are grounded in the pipeline-port spec**
(`docs/superpowers/specs/2026-09-26-pipeline-port-design.md`): adopt its models
(`UserProfile`, `SearchProfile`, `EmailSendLog`), its function contracts
(`prescore`/`skill_match`, `analyze_job`, `write_cover_letter`,
`generate_application`, `send_application`), and its 1:1 ported tests from
`~/Projects/Job_app`. Only that spec's §6 DRF API and §7 `app.html` frontend are
superseded — re-expressed as server-rendered Jobick views/templates (design §8 Q1).
This Plan 1 (the shell) is unaffected by it.

## Global Constraints

- Django **6.0** (pinned `>=6.0,<6.2`); the Jobick template's own `django==5.0`
  pin, `core/` project, `dz.py`, `db.sqlite3`, and its auth/registration pages are
  **discarded** — only its theme (HTML structure + CSS/JS/fonts/images) is reused.
- **No third-party origin in the browser.** All Jobick CSS/JS/fonts/images are
  vendored under the app's `static/`; no CDN tags. `default-src 'self'` stays.
- **Org-scoped reads must be scoped** — `OrgScopedManager` raises `OrgScopeError`
  on unscoped evaluation; every query uses `.for_org(org)` / `.for_user(user)`.
- **One error shape** is for `/api/`; server-rendered pages use Django's own 403/404
  responses, which the existing `handler403/404` already route.
- pytest runs **from `backend/`** on `config.settings.test`.
- No new heavy dependencies in this plan.

## Review Focus

- **Session-less visitor hits `/app/…`** → redirect to `auth.html?next=<original path>`,
  not a 403 and not a server error. (Task 2)
- **Authenticated user with no membership** (edge: bootstrap not yet run) → the
  index renders without raising `OrgScopeError`; shows an empty, honest state.
  (Task 4)
- **`?org=<id>` for an org the user is not a member of** → ignored/forbidden, falls
  back to the user's own oldest membership, never leaks another tenant's rows. (Task 3)
- **Empty data (brand-new workspace)** → KPI tiles render `0` and lists render an
  empty-state block, not broken markup. (Task 4)
- **Unknown `/app/<garbage>` path** → Django 404 via the existing handler, not a 500. (Task 2)

---

### Task 1: Scaffold `apps.dashboard` and mount it at `/app/`

**Files:**
- Create: `backend/apps/dashboard/__init__.py`, `backend/apps/dashboard/apps.py`,
  `backend/apps/dashboard/urls.py`, `backend/apps/dashboard/views.py`
- Create: `backend/apps/dashboard/templates/dashboard_app/base.html` (minimal stub for now)
- Modify: `backend/config/settings/base.py` (add `"apps.dashboard"` to `LOCAL_APPS`)
- Modify: `backend/config/urls.py` (add `path("app/", include("apps.dashboard.urls"))`)
- Test: `backend/tests/dashboard/test_shell.py`

**Interfaces:**
- Produces: URL name `dashboard:index` at `/app/`; `DashboardConfig` (`name = "apps.dashboard"`);
  template namespace `dashboard_app/` (chosen to NOT collide with `apps.common`'s
  `dashboard/`/admin templates).

- [ ] **Step 1: Write the failing test** — `test_app_root_url_resolves` asserts
  `reverse("dashboard:index") == "/app/"`.
- [ ] **Step 2: Run it, expect failure** (`NoReverseMatch` / app not installed).
  Run: `../.venv/bin/pytest tests/dashboard/test_shell.py::test_app_root_url_resolves -v`
- [ ] **Step 3: Implement** the app package, `DashboardConfig`, an `index` view
  returning `render(request, "dashboard_app/index.html", {})` (temporary — Task 4
  fills context), `urls.py` with `app_name = "dashboard"` and
  `path("", views.index, name="index")`; register the app and mount it. Add a
  one-line `base.html` + `index.html` stub so render succeeds.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** — `feat(dashboard): scaffold server-rendered app at /app/`.

---

### Task 2: `session_required` gate → redirect to `auth.html?next=`

**Files:**
- Create: `backend/apps/dashboard/auth.py`
- Modify: `backend/apps/dashboard/views.py` (decorate `index`)
- Test: `backend/tests/dashboard/test_gate.py`

**Interfaces:**
- Produces: `session_required(view_func)` decorator — if
  `request.user.is_authenticated` calls the view, else returns
  `HttpResponseRedirect("/auth.html?next=" + quote(request.get_full_path()))`.

- [ ] **Step 1: Write failing tests** — `test_anonymous_redirected_to_auth`
  (GET `/app/` unauthenticated → 302, `Location` starts with
  `/auth.html?next=%2Fapp%2F`); `test_authenticated_gets_200` (a logged-in client
  → 200); `test_unknown_app_path_404` (GET `/app/nope/` → 404).
- [ ] **Step 2: Run, expect failure** (currently 200 for anonymous).
- [ ] **Step 3: Implement** `session_required` using `urllib.parse.quote`; apply to
  `index`. Confirm `SessionMiddleware`/`AuthenticationMiddleware` are active and that
  OTP verify calls Django `login()` (if it does not, this task's test will show it;
  fix is to add `login(request, user)` in the verify view — note for the implementer,
  do not duplicate auth).
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** — `feat(dashboard): gate /app/ behind OTP session`.

---

### Task 3: Org resolution helper (tenancy rule, safe fallback)

**Files:**
- Create: `backend/apps/dashboard/orgs.py`
- Test: `backend/tests/dashboard/test_orgs.py`

**Interfaces:**
- Consumes: `apps.tenancy` `Membership` (`.for_user(user)`), `Organization`.
- Produces: `resolve_org(request) -> Organization | None` — honors `?org=<id>` /
  `X-FindaJob-Org` **only if** the user has a membership in it; otherwise the user's
  oldest membership; `None` if the user has none.

- [ ] **Step 1: Write failing tests** — `test_oldest_membership_default`,
  `test_org_query_param_honored_when_member`,
  `test_foreign_org_param_falls_back` (param for a non-member org → returns the
  user's own oldest, never the foreign org), `test_no_membership_returns_none`.
  Use the project's existing tenancy test factories/helpers.
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** `resolve_org`, mirroring the `/api/v1/me/` resolution in
  `apps.tenancy` (reuse its helper if one exists rather than re-deriving).
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** — `feat(dashboard): org resolution with safe fallback`.

---

### Task 4: Command-center index context (KPIs + feeds on real data)

**Files:**
- Modify: `backend/apps/dashboard/views.py` (`index` builds context)
- Create: `backend/apps/dashboard/kpis.py`
- Test: `backend/tests/dashboard/test_index_context.py`

**Interfaces:**
- Consumes: `resolve_org` (Task 3); `apps.jobs` `Application` + `ApplicationStatus`,
  `JobListing`.
- Produces: `build_command_center(org, user) -> dict` with keys:
  `kpis` (`{"sent": int, "in_progress": int, "interviews": int}`),
  `recent_jobs` (latest N `JobListing`, org-agnostic global feed),
  `active_applications` (org/user-scoped `Application`s not in a terminal status).
  N default 10. When `org is None`, returns zeroed KPIs and empty lists.

- [ ] **Step 1: Write failing tests** — `test_kpis_count_by_status`
  (seed applications across statuses, assert the three counts),
  `test_empty_workspace_zeroes` (no org / no rows → zeros + empty lists, no raise),
  `test_active_excludes_terminal` (rejected/withdrawn excluded from
  `active_applications`). Map "interviews"/"in_progress"/"sent" to the real
  `ApplicationStatus` members (read the enum; do not invent names).
- [ ] **Step 2: Run, expect failure.**
- [ ] **Step 3: Implement** `build_command_center` (scoped queries via
  `Application.objects.for_org(org)` / `.for_user(user)`), wire into `index`
  context under `cc`.
- [ ] **Step 4: Run, expect pass.**
- [ ] **Step 5: Commit** — `feat(dashboard): command-center KPIs and feeds`.

---

### Task 5: Vendor the Jobick theme and build `base.html` layout

**Files:**
- Create: `backend/apps/dashboard/static/dashboard_app/…` (only the CSS/JS/fonts/
  images the shipped shell + index reference — Bootstrap 5, Jobick theme CSS, icon
  font, logo/images; drop demo-only plugins)
- Modify: `backend/apps/dashboard/templates/dashboard_app/base.html` (full layout:
  left sidebar with the 8 module links, topbar, content block)
- Test: `backend/tests/dashboard/test_layout.py`

**Interfaces:**
- Consumes: Task 1 template namespace.
- Produces: `base.html` blocks `title`, `content`, `extra_css`, `extra_js`; sidebar
  nav items (Dashboard active; Jobs/Applications/Profile & CV/Documents/
  Recommendations/Assistant/Settings as placeholders routing to `#` until their
  plans land).

- [ ] **Step 1: Write failing test** — `test_index_renders_shell` (GET `/app/` as a
  logged-in user → 200, HTML contains the sidebar nav labels and the KPI tile
  values from context; `{% static %}` asset paths resolve under `/static/`).
- [ ] **Step 2: Run, expect failure** (stub base has no sidebar).
- [ ] **Step 3: Implement** by porting Jobick's `admin.html` layout into `base.html`
  (strip `dz.py`/demo references), rewriting asset refs to
  `{% static 'dashboard_app/...' %}`, and fill `index.html` command-center cards
  from the `cc` context. Vendor only referenced assets.
- [ ] **Step 4: Run, expect pass**; also run the whole suite
  (`../.venv/bin/pytest`) to confirm no admin-template collision.
- [ ] **Step 5: Commit** — `feat(dashboard): Jobick layout shell and command-center index`.

---

### Task 6: Auth redirect round-trip (`auth.html?next=` returns to `/app/`)

**Files:**
- Modify: `web/auth.html` and/or `web/api.js` (post-verify redirect honors `next`)
- Test: `backend/tests/dashboard/test_next_contract.py` (server side: the gate emits
  a `next` the frontend can consume) + a note for manual browser verification.

**Interfaces:**
- Consumes: Task 2's redirect contract (`/auth.html?next=<path>`).
- Produces: after successful OTP verify, the browser navigates to the `next` value
  when it is a **same-origin path** (`startsWith("/app/")`); otherwise defaults to
  `/app/`. Open-redirect guard required.

- [ ] **Step 1: Write failing test** — `test_gate_next_is_safe_local_path` asserts the
  gate only ever emits a relative `/app/...` next (no scheme/host), so the frontend
  guard has a safe contract to honor.
- [ ] **Step 2: Run, expect failure** if the gate doesn't yet constrain `next`.
- [ ] **Step 3: Implement** the frontend read of `next` with a same-origin guard in
  `auth.html`/`api.js`; keep `auth.html`'s look unchanged (wiring only, per spec §8a).
- [ ] **Step 4: Run, expect pass**; manually verify the full browser round-trip
  (anonymous `/app/` → auth → OTP → back to `/app/`).
- [ ] **Step 5: Commit** — `feat(auth): return to dashboard next after OTP verify`.

---

## Self-Review

- **Spec coverage (this plan's slice):** server-rendered dashboard (§8 Q1) → Tasks
  1,5; OTP gate + `auth.html?next` interop (§8a) → Tasks 2,6; org scoping (§5, single
  workspace) → Task 3; command center with real, honest data incl. empty state
  (§2, §8 Q6 partial — seed/import is Plan 5) → Task 4. Sidebar module list (§6) →
  Task 5. Profile/matching/chatbot/import are explicitly deferred to Plans 2–5.
- **Review Focus:** all five lines have an owning task's test (Tasks 2,3,4).
- **Type consistency:** `resolve_org` (Task 3) consumed by Task 4; `build_command_center`
  keys fixed in Task 4 and consumed by Task 5's template. `ApplicationStatus` member
  names are read from code, not invented (flagged in Task 4 Step 1).
- **Proportion:** plan carries the decisions (new app, `/app/` mount, `dashboard_app/`
  namespace to avoid admin collision, gate contract, context keys); bodies left to
  the implementer.
