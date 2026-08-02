# Admin dashboard identical to the user dashboard — design

**Date:** 2026-08-02 · **Status:** approved for planning

## Goal

Make the Django Admin visually and structurally identical to the user-facing dashboard
(`web/app.html`, Voltagent theme): the same collapsible icon sidebar on **every** admin page,
and an admin index page that replicates the user dashboard's structure — KPI cards, data-table
chrome — fed by **global platform metrics** (not one org's data).

Decisions made with the operator:

1. **Full layout replication** — not just theming. The admin index becomes a copy of the
   app.html dashboard structure, with real admin data underneath.
2. **KPI data = global platform metrics** via `.unscoped()` (users, organizations, jobs, OTPs
   issued today, pending outbox events, suppressed addresses).
3. **Sidebar on all admin pages** (index, changelists, change forms), entries mapped to the
   real admin changelists.

Design tokens are FINAL and come from `DESIGN-voltagent.md` (repo root) and the
`voltagent-design` skill. Dark canvas `#101010` only; green `#00d992` for active/CTA only;
hairline `#3d3a39` borders, no shadows; Inter + JetBrains Mono; radii 6/8–10px.

## Non-goals

- No change to the public `web/` frontend.
- No new routes, models, migrations, or dependencies.
- No replication of user-only screens (Advisor, Analytics, …) as admin pages — sidebar
  entries map only to what exists in the admin.
- No light mode, ever.

## Architecture

All work lives in `apps/common` (theme owner) plus a small `extra_context` addition on
`FindajobAdminSite`. Existing constraints respected:

- `apps.common` stays pinned **first** in `INSTALLED_APPS` (template loader order).
- `apps.automation` keeps ownership of `index_template`
  (`backend/templates/admin/findajob_index.html`); the n8n card stays included exactly once
  via the agreed hand-off include `admin/_n8n_panel.html`.
- `base_site.html` remains the only place brand chrome is wired; the stylesheet keeps loading
  from `{% block responsive %}` after Django's own CSS.

### 1. Shell + sidebar (every admin page)

The sidebar is injected from the existing `apps/common/templates/admin/base_site.html` via
Django's `{% block nav-sidebar %}` hook (replacing Django's own filter sidebar with
`<nav class="fj-sidebar">`), so no `admin/base.html` fork is needed. CSS turns the container
into the flex row.

- Collapsible 58px (icon-only) ↔ 218px, toggle button at the bottom, state persisted in
  `localStorage` (`fj-admin-sidebar`), initial state icon-only when `window.innerWidth < 900`
  — mirroring app.html's `collapsed: window.innerWidth < 900`.
- Inline SVG stroke icons copied from `web/app.html` (stroke-width 1.8, 17px viewBox usage).
- Entries, in order, mapped to admin URLs with the corresponding app.html icon:
  | Sidebar entry | Icon source (app.html screen) | Target |
  |---|---|---|
  | Dashboard | Dashboard | `admin:index` |
  | Accounts | Profile | `admin:app_list accounts` |
  | Tenancy | Organization | `admin:app_list tenancy` |
  | Legal | Legal | `admin:app_list legal` |
  | Jobs | Job Search | `admin:app_list jobs` |
  | Outbox | Modules | `admin:app_list outbox` |
  | Automation | APIs | `/admin/automation/` (n8n panel) |
  | Auth | Network | `admin:app_list auth` |
- Active row = green primary indicator + tinted fill, exactly the app-shell-row spec
  (`ex-app-shell-row` in DESIGN-voltagent.md).
- Entries render from `available_apps`/permission checks so a staff user without permission
  to an app does not see its entry. A tiny inclusion template + vanilla JS (no framework, no
  `unsafe-eval`) handles the collapse toggle.

### 2. Dashboard index

- `FindajobAdminSite.index()` override: compute global KPI counts with cheap `.count()`
  queries using `.unscoped()` (explicitly permitted in admin) and pass them via
  `extra_context["fj_kpis"]`:
  users, organizations, jobs, OTPs issued today, pending outbox events, suppressed addresses.
  Each KPI degrades to `—` if its model/app is missing (defensive `try/except LookupError`).
- New `apps/common/templates/admin/index.html` that `{% extends %}` the resolved parent and
  renders, **above** the model list: a KPI card row replicating the app.html dashboard cards —
  card chrome 8px radius + hairline, value in JetBrains Mono, uppercase mono eyebrow label,
  3-up grid ≥1024px / 2-up 768–1023 / 1-up <768.
- The admin app/model list restyled as the Voltagent data table (header eyebrow on `#1a1a1a`,
  hairline row borders) — CSS only.
- **Hand-off preserved:** `apps.automation`'s `findajob_index.html` extends `admin/index.html`
  and includes the n8n panel; because our override IS `admin/index.html`, the automation
  template keeps working unchanged and the n8n card renders once, styled as a Voltagent card.

### 3. CSS & fonts

- Append new numbered sections to `apps/common/static/admin/css/voltagent-admin.css`:
  sidebar layout, KPI cards, index table restyle, responsive rules (900px collapse mirror).
- Self-host Inter + JetBrains Mono for the admin by copying the woff2 files from `web/fonts/`
  into `apps/common/static/admin/fonts/` with `@font-face` in the stylesheet. No third-party
  origin is ever contacted.

### 4. Data & security

- All counts are staff-only (admin views), read-only, and use `.unscoped()` deliberately —
  greppable per the tenancy convention.
- No new URL, no API surface, no JS eval; CSP posture of the admin is unchanged.

### 5. Testing

New tests under `backend/tests/admin/` (pytest, run from `backend/`):

1. Index returns 200 for staff and contains the KPI cards with correct counts from fixtures.
2. Sidebar renders on the index **and** on a changelist page; active entry matches the page.
3. `_n8n_panel.html` content appears exactly once on the index.
4. Non-staff/anonymous is redirected to the admin login.
5. A staff user lacking permission to an app does not get that sidebar entry.
6. KPI computation survives an empty database (all zeros, no errors).

## Risks / notes

- Template-loader ordering is the classic silent failure: `apps.common` must stay first in
  `INSTALLED_APPS` (already pinned with a comment in `config/settings/base.py`).
- The `nav-sidebar` block replaces Django's built-in filter sidebar; if any admin page is
  found to depend on it, the fallback is a minimal documented fork of `admin/base.html`
  under a different template name — decided during implementation, noted in the template
  header.
