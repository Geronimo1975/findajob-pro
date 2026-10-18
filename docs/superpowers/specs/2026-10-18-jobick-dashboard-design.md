# Jobick Command-Center Dashboard — Design

**Date:** 2026-10-18
**Status:** Approved by George 2026-10-18 — proceeding to implementation plan
**Author:** George + Claude (brainstorming session)

## 1. Intent and why

findajob.pro's current frontend (the rendered React prototype `app.html` +
`support.js`) shipped and got **zero feedback**. We are doing an atomic pivot:
throw away the prototype shell and rebuild the product around a single
**command-center dashboard** whose first user is George himself — a tool to find
a **nomad / remote job**, drive applications, hold the CV and employment
documents, state what kind of work is wanted, and get **automatic job
recommendations** plus a **secondary chatbot assistant** that points to the right
platforms.

The dashboard is built on the **Jobick** Django admin template (Bootstrap 5,
server-rendered), which George has chosen as the whole UI. The existing findajob
**backend is kept**: OTP login, tenancy/`/me/`, the job adapter registry, the
outbox. The marketing frontend (`index.html`, `unternehmen.html`, `auth.html`)
stays live and is reworked in a later phase (phase 2), after the dashboard works.

**Why now:** the previous shell produced no traction. A focused, usable
command center for one real user (George) is the fastest path to learning what
actually helps, before multiplying it into SaaS.

## 2. Goals

- A working **command-center dashboard** served by the findajob backend, themed
  with Jobick, with ERP-style modules in a left sidebar.
- **Main screen = command center:** KPI tiles at the top (applications sent, in
  progress, interviews), a **feed of new matched nomad jobs** with an "Apply"
  action, and a column showing the status of in-flight applications.
- **Profile & preferences:** George states skills, desired role/work type,
  locations, salary expectations — the inputs that drive matching.
- **Documents module:** upload/manage CV, cover letters, certificates, diplomas
  (the `Document` model already exists).
- **Applications module:** list + status flow (`ApplicationStatus` already
  exists), "copy & mark as sent" for semi-manual platforms, email send via the
  user's own mailbox for `email`/`auto_send` platforms (outbox already exists).
- **Automatic recommendations:** a match score between the candidate profile and
  ingested `JobListing`s, surfaced on the command center and a dedicated
  Recommendations view.
- **Chatbot assistant (secondary module):** backend-mediated **Claude API** call;
  helps George decide where/what to search and which platforms to use. It is a
  side module, not the landing screen.
- Reuse the existing gate: OTP login, `/me/`, org scoping, the one error shape.
  The dashboard is **server-rendered Django** (§8 Q1); it shares the session
  cookie, not the static `api.js` HTTP path.
- **Seed/import of real history:** an import path (management command or admin
  entry) for George's existing applications so KPIs/charts are populated at launch
  (§8 Q6) — no empty dashboard.

## 3. Non-goals (this phase)

- **No multi-tenant product surface.** MVP is a **single workspace — George's
  own**. The tenancy models stay (one Organization, one owner Membership via
  `bootstrap_workspace`), but no invitations, billing, or team UI is built now.
- **No marketing-site rebuild.** `index.html` / `unternehmen.html` / `auth.html`
  stay static and keep their look until phase 2. One accepted exception (§8a):
  `auth.html`'s post-login redirect must learn to return to a dashboard `next`
  URL — a wiring change, not a visual one.
- **No new job sources beyond the LOW-risk adapters** already present (RemoteOK,
  WeWorkRemotely). LinkedIn/Indeed/Wellfound stay semi-manual per the risk table.
- **No automated submission** on LinkedIn/Indeed (hard rule — ban/legal risk).
- **No React/`support.js` prototype work.** It is archived, not extended.

## 4. Anti-goals (would make this a failure even if it "works")

- A dashboard that is pretty but **can't be edited modularly** — the reason we
  are leaving the `.dc.html` React prototype behind. Jobick's server-rendered
  Bootstrap templates must stay hand-editable.
- The chatbot becoming the product. It is a **secondary** helper; if it crowds
  out the command center, we failed the brief.
- Re-introducing a **third-party origin in the browser**. Any Claude call is
  **backend → Anthropic**, never frontend → third party. The `default-src 'self'`
  CSP and the GA4-only consent exception stay intact.
- Shipping invented data as if real — recommendations/matches must be computed
  from real ingested jobs and the real profile, or shown as empty, never faked.
- Breaking the legal surface (real Impressum, consent-at-verify, `seed_legal`).

## 5. Current state we build on

Backend (kept, mostly already modelled):
- `apps.accounts` — OTP login, `EmailOTP`, `SuppressedAddress`.
- `apps.tenancy` — `Organization`, `Membership`, `AuditLog`, `OrgScopedManager`,
  the single `GET /api/v1/me/`.
- `apps.jobs` — `Platform`, `JobListing`, **`Document`**, **`Application`** +
  `ApplicationStatus`, **`ApplicationDocument`**; adapter registry (RemoteOK,
  WeWorkRemotely); `seed_platforms`, `ingest_jobs`.
- `apps.tenancy.outbox` — `MailboxConnection`, `/outbox/mailbox/` (send from the
  user's own mailbox).
- `apps.legal`, `apps.common` (error shape, custom admin), `apps.leads`,
  `apps.automation`.

Endpoints that **really exist** today: `auth/*`, `/me/`, `/platforms/`,
`/jobs/`, `/legal/*`, `/consent/*`, `/outbox/mailbox/*`, `/leads/*`, `i18n`.
Endpoints in `web/api.js` that are **aspirational (not built)**: `generate`,
`send`, `documents`, `profile`, `integrations`, `contacts`, `org`, `billing`,
`skillmatch`.

**Reuse the existing pipeline-port spec.** `docs/superpowers/specs/2026-09-26-pipeline-port-design.md`
already specifies how to port the working `Job_app` CLI pipeline into findajob
(matching, AI analysis, cover-letter + email + PDF generation, SMTP send,
`UserProfile`/`SearchProfile`/`EmailSendLog` models, and 1:1 test cases). Its
**backend logic is UI-agnostic and governs Plans 2–5**; only its §6 (DRF API) and
§7 (`app.html` frontend) are superseded by this spec's server-rendered Jobick
decision (§8 Q1) and get re-expressed as server-rendered views/templates. Modules
to import from `~/Projects/Job_app` (per that spec): `filters/relevance.py` →
`apps/jobs/matching.py`; `agents/analyzer_agent.py` + `generators/cover_letter_gen.py`
→ `apps/jobs/ai.py`; `generators/email_body_gen.py` → `apps/jobs/generation.py`;
`generators/pdf_gen.py` → `apps/jobs/pdf.py`; `sender/smtp_sender.build_email` →
`apps/tenancy/outbox/services.py`; plus `anthropic`/`fpdf2`/`pypdf` deps and the
portable tests. Excluded there and here: JobSpy (LinkedIn/Indeed), n8n, Google
Sheet, Excel tracker. `Job_app/dashboard/generation_bridge.py` is a useful
reference for wiring the pipeline to a Django dashboard, not a port target.

**Missing and to be built:** a candidate **Profile/Preferences** model (skills,
desired work type, locations, salary, remote-only flag); the **match/scoring**
logic; the **chatbot** endpoint; and DRF views/serializers (or server-rendered
views — see §6) for documents, applications, profile, recommendations, send,
and the chatbot.

Jobick template (source `Jobify/…jobick-django…zip`): Django 5.0 + Bootstrap,
server-rendered. Ready screens that map ~1:1: `index` (candidate KPI dashboard +
charts), `jobs/{job-list,job-view,apply-job,job-application,user-profile}`,
`my-profile`/`edit-profile`, `app-calendar`, `email`, `charts`, `forms`,
`table`. **Discard** from Jobick: `dz.py` (dead platform), its own `core`
project/settings, `db.sqlite3`, and its auth/registration pages (we use OTP).

## 6. The parts of the "how" already decided

- **UI framework:** Jobick (Bootstrap 5, server-rendered Django templates). Theme
  = Jobick dark/light as shipped; no React prototype.
- **Sidebar modules (ERP-style, left):** Dashboard · Jobs · My Applications ·
  Profile & CV · Documents · Recommendations · Assistant (chatbot) · Settings.
- **Landing screen:** command center (KPIs + matched-jobs feed + in-flight
  applications). Chatbot is a secondary module, not the landing.
- **MVP audience:** single workspace (George). Multi-tenant deferred.
- **Recommendations:** automatic **profile → job match score**, reusing the
  `skill-match` idea already sketched in `api.js`.
- **Chatbot brain:** **Claude API**, called from the backend (new dependency +
  API key + per-message cost, accepted). Latest capable model per house default.
- **Auth/gate unchanged:** OTP login, `/me/`, org scoping, one error shape.
- **Keep the hard rules:** no CDN/third-party origin in the browser; no auto-submit
  on LinkedIn/Indeed; real Impressum; `documents/` real-CV dir never committed.

## 7. Left to the builder (plan decides)

- **Integration shape — the key open call (see §8 Q1):** whether Jobick's screens
  are rendered **server-side as Django templates** (Jobick's native mode, new
  server-rendered views + context, `web/api.js` recedes), or whether we keep the
  **static + DRF** split and reskin with Jobick's CSS/markup as static pages that
  call the API. Default recommendation: server-rendered Django templates for the
  dashboard (matches Jobick, simplest to edit), behind the same OTP gate.
- Exact `Profile`/`Preferences` model fields and whether it lives in `apps.jobs`
  or a new `apps.profiles` app.
- The matching algorithm (keyword/tag overlap vs weighted skill scoring) and
  where it runs (request-time vs precomputed on ingest).
- Chatbot grounding: how much job/profile context is injected into the prompt,
  conversation persistence, rate/cost limits.
- Which Jobick static assets are vendored under `web/`/`static/` vs trimmed.
- Migration/data story for the single existing workspace.
- Test strategy (pytest from `backend/`, per house convention).

## 7a. Builder-check calls already settled (Claude's call, not George's)

These were raised by the builder-check and settled as sensible defaults; a plan
can rely on them without changing what ships:

- **Profile model home:** a new `apps.profiles` app, org-scoped via
  `OrgScopedManager`, one row per (org, user). Fields: `skills` (JSON list),
  `desired_roles` (JSON), `work_type`, `locations` (JSON), `salary_min` +
  `currency`, `remote_only` (bool).
- **Match v1:** computed at request time — tag/skill overlap (Jaccard) + title
  keyword hits, normalized 0–100, no caching until it is measurably slow. Because
  `JobListing` is shared across tenants, the score is **not** stored on it; if
  precompute is ever needed, a small `JobMatch(org, user, listing, score)` table.
- **Jobick assets:** vendor only what the shipped screens reference (Bootstrap 5,
  Jobick theme CSS, its icon font/images); drop demo-only plugins.
- **Tests:** pytest from `backend/` on `config.settings.test` (house rule).
- **Single-workspace seed:** George's Org + owner Membership come from
  `bootstrap_workspace` at first OTP login; a management command/fixture seeds his
  profile row.
- **i18n:** dashboard templates use `{% trans %}`/`gettext`; actual DE/RO copy is
  deferred (George is the only, EN, user this phase).

## 8. Decisions from George (2026-10-18)

- **Q1 — Rendering (the big one): server-rendered Django templates.** The
  dashboard is built as Jobick's native Django views rendering HTML on the
  server, behind the OTP session. This **deliberately abandons the
  static-frontend + nginx + DRF + single-`api.js`-HTTP-owner architecture *for the
  dashboard*.** CLAUDE.md's "UI-first / static frontend" pillars still govern the
  **marketing site** (`index.html`, `unternehmen.html`, `auth.html`), which stays
  static; the dashboard is a new server-rendered surface alongside it. CLAUDE.md
  must be updated to record this split once the dashboard lands.
- **Q4 — "Nomad" = remote within EU time zones.** The ingest filter and the match
  inputs target 100% remote roles restricted to EU time zones / EU-friendly
  companies (overlap with George's working hours). Not worldwide, not relocation.
- **Q3 — Chatbot: persist history, Anthropic dependency approved.** Conversations
  are stored (org-scoped `ChatMessage`/`ChatThread`). The Anthropic SDK + an API
  key in env are approved (key never logged; `RedactingFilter` is the backstop).
  A **daily cost cap** is set; past it the assistant shows "unavailable" rather
  than failing hard. Real profile/job context may be injected into the prompt
  (acceptable for a single self-user; revisit before multi-tenant).
- **Q6 — Seed data: yes.** The dashboard must **not** launch with empty KPIs/charts.
  George will import a few real existing applications / history so the command
  center looks populated from day one. The plan needs an import path (management
  command or admin entry) for George's real application history.

## 8b. Enriched command center + dark theme (George, 2026-10-18, after seeing Plan 1)

New requirements for the dashboard, built as **Plan 2**:
- **Dark theme is the home, with a toggle to light that remembers the choice.** The dark
  variant is styled to the **Voltagent design system** (`voltagent-design` skill: canvas
  `#101010`, hairline borders `#3d3a39`, accent green `#00d992` for CTA/status only, Inter +
  JetBrains Mono) — not Jobick's generic Bootstrap dark — because that is findajob's identity
  (the design system is dark-first). Light = Jobick's light, secondary. SSR default = dark;
  the toggle writes `localStorage` + a `data-theme` attribute so there is no flash.
- **Phase-1 command-center blocks:** (a) a **Documents** summary (what documents/CVs you have,
  by kind), (b) **Recommendations** (top matched nomad jobs), (c) a **main jobs table** — nomad
  jobs with **match %** (title, company, platform, match %, salary, status, action), inspired by
  the old findajob dashboard and `Job_app`, and (d) an **internal automation table** showing
  **job-ingestion runs** (what the automated pipeline scraped: per platform, new count, when).
- **Match % requires matching + a profile.** Port the deterministic matcher from `Job_app`
  (`filters/relevance.py` → `apps/jobs/matching.py`: `prescore`, `skill_tokens`, `skill_match`)
  with its tests, per the pipeline-port spec §5/§10. Add `apps.profiles` with `SearchProfile`
  (pipeline-port §4), lazily created; an empty profile yields a neutral match (no crash). This
  pulls matching forward from the old Plan 3 into Plan 2 because the main table needs it.
- **Automation table data (phase 1):** derive ingestion runs from existing `JobListing` data
  (per `platform`: latest `scraped_at` + count in the last window) rather than a new model —
  honest and cheap; a dedicated `IngestionRun` model is a later refinement.
- `Job_app` is inspiration, not a drop-in: port the pure functions and tests, re-express the UI.
- UI work must invoke `voltagent-design` + `ui-ux-pro-max` before writing templates/CSS.

## 8a. Gate & interop calls settled from Q1 (builder's call)

- **Session gate on rendered pages:** server-rendered dashboard views are guarded
  by a `login_required`-style decorator that reads the **same session cookie** set
  by the existing OTP verify flow. No duplicate auth.
- **Login for a session-less visitor:** the four `/api/v1/auth/*` JSON routes and
  the static `auth.html` stay the login surface. A dashboard view hit without a
  session **redirects to `auth.html?next=<dashboard-url>`**; on successful OTP
  verify the browser is sent back to `next`. Jobick's own login/register pages are
  discarded. This means **phase 1 does touch the auth redirect contract** (not the
  look of `auth.html`, just where it returns to) — an accepted, small exception to
  "marketing untouched."
