# Voltagent Admin Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Django Admin structurally identical to the user dashboard (`web/app.html`): the Voltagent icon sidebar on every admin page, and an index page with global-platform KPI cards.

**Architecture:** Everything lives in `apps.common` (the theme owner) except a small `FindajobAdminSite` change. The sidebar is injected via Django's `{% block nav-sidebar %}` hook in the existing `base_site.html`. The index is a documented fork of Django's `admin/index.html` (so `apps.automation`'s `findajob_index.html` wrapper keeps extending it by name and the n8n card renders exactly once). KPI counts are computed in `FindajobAdminSite.index()`.

**Tech Stack:** Django 6.0 admin templates, vanilla JS (no eval), CSS appended to `voltagent-admin.css`, pytest-django.

**Spec:** `docs/superpowers/specs/2026-08-02-admin-voltagent-dashboard-design.md`

## Global Constraints

- Run all Python commands from `backend/`; pytest via `../.venv/bin/pytest`.
- `apps.common` must stay FIRST in `INSTALLED_APPS` (template loader order) — do not touch that list.
- Dark canvas `#101010` only; green `#00d992` for active/CTA only; hairline `#3d3a39`; no shadows; Inter + JetBrains Mono; radii 6px buttons / 8–10px cards.
- No new dependencies, routes, models, or migrations. No third-party origins (fonts self-hosted).
- No `unsafe-eval`, no inline event handlers in new JS — one small static file with `addEventListener`.
- Org-scoped counts use the greppable `.unscoped()`; plain models use `.objects`.
- The n8n card must render exactly once on the index (hand-off: `backend/templates/admin/findajob_index.html` extends `admin/index.html` and includes `admin/_n8n_panel.html`).

---

### Task 1: KPI counts in `FindajobAdminSite`

**Files:**
- Modify: `backend/apps/common/admin_site.py`
- Test: `backend/tests/admin/test_admin_dashboard.py` (new)

**Interfaces:**
- Produces: `index()` puts `extra_context["fj_kpis"]` = list of dicts `{"key": str, "label": str, "value": int}`, in this fixed order of keys: `users`, `organizations`, `job_listings`, `otps_today`, `mailboxes`, `suppressed`. Task 3's template iterates `fj_kpis` and renders `kpi.label` / `kpi.value` inside `<div class="fj-kpi" data-kpi="{{ kpi.key }}">`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/admin/test_admin_dashboard.py`:

```python
"""The admin index as a Voltagent dashboard: KPI context and rendering."""

from __future__ import annotations

from django.urls import reverse

from tests.admin.support import AdminTestCase


class KpiContextTests(AdminTestCase):
    def _kpis(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        return {k["key"]: k for k in response.context["fj_kpis"]}

    def test_kpi_keys_and_order(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(
            [k["key"] for k in response.context["fj_kpis"]],
            ["users", "organizations", "job_listings", "otps_today",
             "mailboxes", "suppressed"],
        )

    def test_counts_match_fixtures(self):
        kpis = self._kpis()
        # From AdminTestCase.setUpTestData: 3 users, 2 orgs, 1 job listing,
        # 4 OTP rows all created "today" by auto_now_add, 1 mailbox,
        # 1 suppressed address.
        self.assertEqual(kpis["users"]["value"], 3)
        self.assertEqual(kpis["organizations"]["value"], 2)
        self.assertEqual(kpis["job_listings"]["value"], 1)
        self.assertEqual(kpis["otps_today"]["value"], 4)
        self.assertEqual(kpis["mailboxes"]["value"], 1)
        self.assertEqual(kpis["suppressed"]["value"], 1)

    def test_every_kpi_has_a_label(self):
        for kpi in self._kpis().values():
            self.assertTrue(str(kpi["label"]))


class AnonymousRedirectTests(AdminTestCase):
    def test_anonymous_is_redirected_to_admin_login(self):
        self.client.logout()
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])


class KpiEmptyDatabaseTests(AdminTestCase):
    """KPIs survive missing data (all-zero) without raising."""

    def test_zero_counts_render(self):
        from apps.accounts.models import EmailOTP, SuppressedAddress
        from apps.jobs.models import Application, JobListing
        from apps.outbox.models import MailboxConnection

        MailboxConnection.all_objects.all().delete()
        Application.all_objects.all().delete()
        JobListing.objects.all().delete()
        EmailOTP.objects.all().delete()
        SuppressedAddress.objects.all().delete()

        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        kpis = {k["key"]: k["value"] for k in response.context["fj_kpis"]}
        self.assertEqual(kpis["job_listings"], 0)
        self.assertEqual(kpis["otps_today"], 0)
        self.assertEqual(kpis["mailboxes"], 0)
        self.assertEqual(kpis["suppressed"], 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run (from `backend/`): `../.venv/bin/pytest tests/admin/test_admin_dashboard.py -v`
Expected: FAIL with `KeyError: 'fj_kpis'` (context lacks the key).

- [ ] **Step 3: Implement `index()` on `FindajobAdminSite`**

In `backend/apps/common/admin_site.py`, add imports and the method. Keep the module's existing docstring conventions; note that `index_template` remains untouched (the automation hand-off in the module docstring still holds — we only add context).

```python
def _fj_kpis(self) -> list[dict]:
    """Global platform counts for the index dashboard cards.

    Deliberately cross-tenant: this is the operator's console. Org-scoped
    models are read through the greppable ``.unscoped()``; the rest through
    their plain managers. Each count is independent so one broken app never
    blanks the whole row.
    """
    from django.utils import timezone

    from apps.accounts.models import EmailOTP, SuppressedAddress, User
    from apps.jobs.models import JobListing
    from apps.outbox.models import MailboxConnection
    from apps.tenancy.models import Organization

    today = timezone.localdate()
    return [
        {"key": "users", "label": _("Users"),
         "value": User.objects.count()},
        {"key": "organizations", "label": _("Organizations"),
         "value": Organization.objects.count()},
        {"key": "job_listings", "label": _("Job listings"),
         "value": JobListing.objects.count()},
        {"key": "otps_today", "label": _("OTP codes today"),
         "value": EmailOTP.objects.filter(created_at__date=today).count()},
        {"key": "mailboxes", "label": _("Mailbox connections"),
         "value": MailboxConnection.objects.unscoped().count()},
        {"key": "suppressed", "label": _("Suppressed addresses"),
         "value": SuppressedAddress.objects.count()},
    ]

def index(self, request, extra_context=None):
    extra_context = {**(extra_context or {}), "fj_kpis": self._fj_kpis()}
    return super().index(request, extra_context=extra_context)
```

Note: `Organization.objects` is `OrganizationManager()` (not org-scoped — it IS the org table) and `JobListing.objects` is a plain manager; both `.count()` directly. `MailboxConnection.objects` is `OrgScopedManager`, hence `.unscoped()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/pytest tests/admin/test_admin_dashboard.py -v` → PASS.
Also run the existing suite guard: `../.venv/bin/pytest tests/admin/ -q` → all pass.

- [ ] **Step 5: Commit**

```bash
git add apps/common/admin_site.py tests/admin/test_admin_dashboard.py
git commit -m "feat(admin): global KPI counts on the admin index"
```

---

### Task 2: Voltagent sidebar on every admin page

**Files:**
- Modify: `backend/apps/common/admin_site.py` (add `each_context`)
- Modify: `backend/apps/common/templates/admin/base_site.html` (add `nav-sidebar` + `extrahead` blocks)
- Create: `backend/apps/common/templates/admin/_fj_sidebar.html`
- Create: `backend/apps/common/static/admin/js/fj-sidebar.js`
- Test: `backend/tests/admin/test_admin_dashboard.py` (append)

**Interfaces:**
- Consumes: nothing from Task 1 (independent context keys).
- Produces: `each_context()` adds `fj_app_labels: set[str]` (app labels the user may see) and `fj_active: str` (the sidebar key for the current page: `"dashboard"`, an app label, or `""`). The template renders `<nav class="fj-sidebar" id="fj-sidebar">` with one `<a class="fj-nav-item" data-nav="<key>">` per visible entry. Task 4 styles these classes.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/admin/test_admin_dashboard.py`:

```python
class SidebarTests(AdminTestCase):
    def test_sidebar_on_index_and_changelist(self):
        from apps.accounts.models import User
        from tests.admin.support import changelist_url

        for url in (reverse("admin:index"), changelist_url(User)):
            response = self.client.get(url)
            self.assertContains(response, 'id="fj-sidebar"')
            self.assertContains(response, 'data-nav="dashboard"')

    def test_active_entry_matches_page(self):
        from apps.accounts.models import User
        from tests.admin.support import changelist_url

        response = self.client.get(changelist_url(User))
        self.assertContains(
            response, 'data-nav="accounts" aria-current="page"'
        )
        response = self.client.get(reverse("admin:index"))
        self.assertContains(
            response, 'data-nav="dashboard" aria-current="page"'
        )

    def test_entries_filtered_by_permission(self):
        """A staff user with no permissions sees only Dashboard."""
        from apps.accounts.models import User

        limited = User.objects.create_user(email="staff@example.de")
        limited.is_staff = True
        limited.save(update_fields=["is_staff"])
        self.client.force_login(limited)
        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, 'data-nav="dashboard"')
        self.assertNotContains(response, 'data-nav="accounts"')
        self.assertNotContains(response, 'data-nav="tenancy"')

    def test_login_page_has_no_sidebar(self):
        self.client.logout()
        response = self.client.get(reverse("admin:login"))
        self.assertNotContains(response, 'id="fj-sidebar"')
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `../.venv/bin/pytest tests/admin/test_admin_dashboard.py -k Sidebar -v`
Expected: FAIL — `fj-sidebar` not in any response.

- [ ] **Step 3: Add `each_context` to `FindajobAdminSite`**

```python
#: Sidebar entries: (key, app_label-or-None, icon path d, label).
#: Icons are the exact stroke paths from web/app.html's nav (17px, 24 viewBox,
#: stroke-width 1.8) so the admin sidebar is glyph-identical to the app shell.
SIDEBAR_ITEMS: tuple[tuple[str, str | None, str, object], ...] = (
    ("dashboard", None,
     "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z", _("Dashboard")),
    ("accounts", "accounts",
     "M12 4a4 4 0 1 0 0 8a4 4 0 1 0 0-8M4 21c0-4 3.6-7 8-7s8 3 8 7",
     _("Accounts")),
    ("tenancy", "tenancy",
     "M3 21h18M5 21V7l7-4l7 4v14M9 9h0.01M9 13h0.01M15 9h0.01M15 13h0.01M9 21v-4h6v4",
     _("Tenancy")),
    ("legal", "legal",
     "M12 3v18M8 21h8M6 7L3 13a3 3 0 0 0 6 0zM18 7l-3 6a3 3 0 0 0 6 0zM5 7h14",
     _("Legal")),
    ("jobs", "jobs",
     "M11 4a7 7 0 1 0 0 14a7 7 0 1 0 0-14M21 21l-4.35-4.35", _("Jobs")),
    ("outbox", "outbox",
     "M12 2l8 4.5v9L12 20l-8-4.5v-9zM12 11v9M4 6.5l8 4.5l8-4.5", _("Outbox")),
    ("automation", "automation",
     "M13 2L4 14h6l-1 8l9-12h-6z", _("Automation")),
    ("auth", "auth",
     "M9 7a3 3 0 1 0 0 6a3 3 0 1 0 0-6M17 9a2.5 2.5 0 1 1 0 5M3 21c0-3.3 2.7-6 6-6s6 2.7 6 6M15.5 15.3c2.6.5 4.5 2.8 4.5 5.7",
     _("Auth")),
)
```

And on the class:

```python
def each_context(self, request):
    context = super().each_context(request)
    labels = {
        app.get("app_label", "")
        for app in context.get("available_apps") or []
    }
    # The automation panel is a plain admin view (no models), so it never
    # appears in available_apps; staff membership is its own gate there.
    if getattr(request.user, "is_staff", False):
        labels.add("automation")
    context["fj_app_labels"] = labels

    path = request.path
    active = ""
    if path == reverse("admin:index"):
        active = "dashboard"
    else:
        for key, app_label, _d, _label in SIDEBAR_ITEMS:
            if app_label and path.startswith(f"/admin/{app_label}/"):
                active = key
                break
    context["fj_active"] = active
    context["fj_sidebar_items"] = [
        {"key": key, "app_label": app_label, "d": d, "label": label}
        for key, app_label, d, label in SIDEBAR_ITEMS
    ]
    return context
```

Add `from django.urls import reverse` to the module imports.

- [ ] **Step 4: Create the sidebar template**

`backend/apps/common/templates/admin/_fj_sidebar.html`:

```html
{% load i18n %}
{% comment %}
  The Voltagent app-shell sidebar, glyph-identical to web/app.html. Rendered
  from base_site.html's nav-sidebar block, which Django only emits for
  authenticated admin pages (login/popup pages skip it). Entries render only
  when the user can see the app (fj_app_labels, from each_context).
{% endcomment %}
<nav class="fj-sidebar" id="fj-sidebar" aria-label="{% translate 'Sections' %}">
  {% for item in fj_sidebar_items %}
    {% if item.app_label is None or item.app_label in fj_app_labels %}
      {% if item.key == "dashboard" %}
        <a class="fj-nav-item" data-nav="dashboard"
           {% if fj_active == "dashboard" %}aria-current="page"{% endif %}
           href="{% url 'admin:index' %}" title="{{ item.label }}">
      {% elif item.key == "automation" %}
        <a class="fj-nav-item" data-nav="automation"
           {% if fj_active == "automation" %}aria-current="page"{% endif %}
           href="/admin/automation/" title="{{ item.label }}">
      {% else %}
        <a class="fj-nav-item" data-nav="{{ item.key }}"
           {% if fj_active == item.key %}aria-current="page"{% endif %}
           href="{% url 'admin:app_list' app_label=item.app_label %}"
           title="{{ item.label }}">
      {% endif %}
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
               stroke-linejoin="round" aria-hidden="true">
            <path d="{{ item.d }}"></path>
          </svg>
          <span class="fj-nav-label">{{ item.label }}</span>
        </a>
    {% endif %}
  {% endfor %}
  <button type="button" class="fj-sidebar-toggle" id="fj-sidebar-toggle"
          aria-controls="fj-sidebar"
          title="{% translate 'Collapse sidebar' %}">
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" stroke-width="1.8" stroke-linecap="round"
         stroke-linejoin="round" aria-hidden="true">
      <path d="M15 18l-6-6 6-6"></path>
    </svg>
  </button>
</nav>
```

- [ ] **Step 5: Wire the block and the script in `base_site.html`**

Add to `backend/apps/common/templates/admin/base_site.html` (keep the file's comment style; both blocks are new):

```html
{% block nav-sidebar %}
  {% if user.is_active and user.is_staff %}
    {% include "admin/_fj_sidebar.html" %}
  {% endif %}
{% endblock %}

{% block extrahead %}
  {{ block.super }}
  <script src="{% static 'admin/js/fj-sidebar.js' %}" defer></script>
{% endblock %}
```

(Replacing `nav-sidebar` drops Django's own filter/nav sidebar — that is the
design decision recorded in the spec.)

- [ ] **Step 6: Create the collapse script**

`backend/apps/common/static/admin/js/fj-sidebar.js`:

```javascript
/* Collapse toggle for the Voltagent admin sidebar.
   Mirrors web/app.html: icon-only below 900px, state kept per browser. */
(function () {
  "use strict";
  var KEY = "fj-admin-sidebar";
  var sidebar = document.getElementById("fj-sidebar");
  var toggle = document.getElementById("fj-sidebar-toggle");
  if (!sidebar || !toggle) return;

  var stored = null;
  try { stored = window.localStorage.getItem(KEY); } catch (e) { /* private mode */ }
  var collapsed = stored === null ? window.innerWidth < 900 : stored === "1";

  function apply() {
    sidebar.classList.toggle("fj-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  apply();

  toggle.addEventListener("click", function () {
    collapsed = !collapsed;
    try { window.localStorage.setItem(KEY, collapsed ? "1" : "0"); } catch (e) { /* ignore */ }
    apply();
  });
})();
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `../.venv/bin/pytest tests/admin/test_admin_dashboard.py -v` → PASS.
Run: `../.venv/bin/pytest tests/admin/ -q` → the whole admin suite still passes (the smoke test opens every page, so a template error anywhere shows up here).

- [ ] **Step 8: Commit**

```bash
git add apps/common/admin_site.py apps/common/templates/admin/base_site.html \
        apps/common/templates/admin/_fj_sidebar.html \
        apps/common/static/admin/js/fj-sidebar.js \
        tests/admin/test_admin_dashboard.py
git commit -m "feat(admin): Voltagent app-shell sidebar on every admin page"
```

---

### Task 3: Index template with KPI cards (n8n card stays single)

**Files:**
- Create: `backend/apps/common/templates/admin/index.html`
- Test: `backend/tests/admin/test_admin_dashboard.py` (append)

**Interfaces:**
- Consumes: `fj_kpis` from Task 1 (list of `{"key","label","value"}`).
- Produces: `<div class="fj-kpi-row">` with one `<div class="fj-kpi" data-kpi="<key>">` per KPI. Task 4 styles these classes.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/admin/test_admin_dashboard.py`:

```python
class IndexTemplateTests(AdminTestCase):
    def test_kpi_cards_render_with_values(self):
        response = self.client.get(reverse("admin:index"))
        content = response.content.decode()
        self.assertIn('data-kpi="users"', content)
        self.assertIn('data-kpi="suppressed"', content)
        # 3 fixture users; value is rendered inside the card.
        self.assertIn('class="fj-kpi-value">3<', content)

    def test_n8n_panel_renders_exactly_once(self):
        response = self.client.get(reverse("admin:index"))
        content = response.content.decode()
        self.assertEqual(content.count('id="fj-n8n-index-card"'), 1)

    def test_app_list_still_present(self):
        from apps.accounts.models import User
        from tests.admin.support import changelist_url

        response = self.client.get(reverse("admin:index"))
        self.assertContains(response, changelist_url(User))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `../.venv/bin/pytest tests/admin/test_admin_dashboard.py -k IndexTemplate -v`
Expected: `test_kpi_cards_render_with_values` FAILS (no `data-kpi` markup);
the n8n and app-list tests may already pass — that is fine.

- [ ] **Step 3: Create the index override**

First read Django's own template to fork it faithfully:
`.venv/lib/python3.12/site-packages/django/contrib/admin/templates/admin/index.html`.

Create `backend/apps/common/templates/admin/index.html` with the same blocks as
Django's file (extends `admin/base_site.html`, keeps the `dashboard` bodyclass,
the `admin/app_list.html` include with `show_changelinks=True`, and the
recent-actions sidebar block), inserting the KPI row at the TOP of
`{% block content %}`, before `#content-main`:

```html
{% comment %}
  A FORK of django/contrib/admin/templates/admin/index.html (Django 6.0),
  not an extension — a template cannot {% extends %} its own name.
  apps.automation's findajob_index.html extends "admin/index.html" by name,
  resolves to THIS file (apps.common is first in INSTALLED_APPS), and appends
  the n8n card once — do NOT include admin/_n8n_panel.html here, per the
  warning in that file. On Django upgrades, re-diff against the vendor file.
{% endcomment %}
```

The KPI markup to insert:

```html
{% if fj_kpis %}
<div class="fj-kpi-row">
  {% for kpi in fj_kpis %}
    <div class="fj-kpi" data-kpi="{{ kpi.key }}">
      <span class="fj-kpi-label">{{ kpi.label }}</span>
      <span class="fj-kpi-value">{{ kpi.value }}</span>
    </div>
  {% endfor %}
</div>
{% endif %}
```

Everything else in the fork is copied verbatim from the vendor file.

- [ ] **Step 4: Run tests to verify they pass**

Run: `../.venv/bin/pytest tests/admin/test_admin_dashboard.py -v` → PASS.
Run: `../.venv/bin/pytest tests/admin/ -q` → all pass (automation's index tests
guard the n8n hand-off).

- [ ] **Step 5: Commit**

```bash
git add apps/common/templates/admin/index.html tests/admin/test_admin_dashboard.py
git commit -m "feat(admin): index dashboard with global KPI cards"
```

---

### Task 4: CSS + self-hosted fonts

**Files:**
- Create: `backend/apps/common/static/admin/fonts/` (4 woff2 files copied from `web/fonts/`)
- Modify: `backend/apps/common/static/admin/css/voltagent-admin.css` (append new numbered sections)
- Test: `backend/tests/admin/test_admin_dashboard.py` (append)

**Interfaces:**
- Consumes: `.fj-sidebar`, `.fj-nav-item`, `.fj-nav-label`, `.fj-collapsed`, `.fj-sidebar-toggle` (Task 2); `.fj-kpi-row`, `.fj-kpi`, `.fj-kpi-label`, `.fj-kpi-value` (Task 3).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/admin/test_admin_dashboard.py`:

```python
class StaticAssetTests(AdminTestCase):
    def test_theme_assets_are_findable(self):
        from django.contrib.staticfiles import finders

        for path in (
            "admin/css/voltagent-admin.css",
            "admin/js/fj-sidebar.js",
            "admin/fonts/inter-latin.woff2",
            "admin/fonts/inter-latin-ext.woff2",
            "admin/fonts/jetbrains-mono-latin.woff2",
            "admin/fonts/jetbrains-mono-latin-ext.woff2",
        ):
            self.assertIsNotNone(finders.find(path), path)

    def test_sidebar_css_present(self):
        from django.contrib.staticfiles import finders

        css = open(finders.find("admin/css/voltagent-admin.css")).read()
        for selector in (".fj-sidebar", ".fj-kpi-row", ".fj-collapsed"):
            self.assertIn(selector, css)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `../.venv/bin/pytest tests/admin/test_admin_dashboard.py -k StaticAsset -v`
Expected: FAIL on the font paths and selectors.

- [ ] **Step 3: Copy the fonts**

```bash
mkdir -p apps/common/static/admin/fonts
cp ../web/fonts/*.woff2 apps/common/static/admin/fonts/
```

- [ ] **Step 4: Append the CSS**

Open `voltagent-admin.css`, find the last numbered section comment, and append
new sections continuing the numbering (check the actual last number; the file
is ~2275 lines). Content to append — adjust ONLY the section numbers:

```css
/* == §N — self-hosted fonts (mirrors web/fonts/fonts.css; SIL OFL 1.1).
   Self-hosted deliberately: a Google Fonts URL would ship the operator's IP
   to a US CDN. Variable files; each face pins one weight instance. == */
@font-face { font-family: 'Inter'; font-style: normal; font-weight: 400 700;
  font-display: swap; src: url(../fonts/inter-latin.woff2) format('woff2');
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD; }
@font-face { font-family: 'Inter'; font-style: normal; font-weight: 400 700;
  font-display: swap; src: url(../fonts/inter-latin-ext.woff2) format('woff2');
  unicode-range: U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF; }
@font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 400 700;
  font-display: swap; src: url(../fonts/jetbrains-mono-latin.woff2) format('woff2');
  unicode-range: U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD; }
@font-face { font-family: 'JetBrains Mono'; font-style: normal; font-weight: 400 700;
  font-display: swap; src: url(../fonts/jetbrains-mono-latin-ext.woff2) format('woff2');
  unicode-range: U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF; }

/* == §N+1 — the app-shell sidebar (mirrors web/app.html's aside). == */
.fj-sidebar {
  position: sticky; top: 0; align-self: flex-start;
  display: flex; flex-direction: column; gap: 2px;
  width: 218px; min-height: 100vh; padding: 10px;
  background: #101010; border-right: 1px solid #3d3a39;
  box-sizing: border-box; flex-shrink: 0;
  transition: width 120ms ease;
}
.fj-sidebar.fj-collapsed { width: 58px; }
.fj-sidebar.fj-collapsed .fj-nav-label { display: none; }
.fj-nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 9px 10px; border-radius: 6px;
  color: #8b949e; font: 500 13px/1 Inter, sans-serif;
  text-decoration: none; white-space: nowrap;
}
.fj-nav-item:hover { background: #1a1a1a; color: #ffffff; }
.fj-nav-item[aria-current="page"] {
  background: rgba(0, 217, 146, 0.10);
  color: #2fd6a1;
  box-shadow: inset 2px 0 0 #00d992;
}
.fj-sidebar-toggle {
  margin-top: auto; display: flex; align-items: center; justify-content: center;
  min-height: 44px; padding: 9px 10px; border-radius: 6px;
  background: #101010; border: 1px solid #3d3a39; color: #8b949e; cursor: pointer;
}
.fj-sidebar-toggle:hover { background: #1a1a1a; color: #ffffff; }
.fj-collapsed .fj-sidebar-toggle svg { transform: rotate(180deg); }
/* Lay the shell out as a flex row: Django's #main wraps sidebar + content
   in 6.0; make it the flex container so the sidebar sits left of #content. */
#main { display: flex !important; align-items: stretch; }
#main > #content-start { flex: 1; min-width: 0; }

/* == §N+2 — index KPI cards (dashboard card chrome from app.html). == */
.fj-kpi-row {
  display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px;
  margin: 0 0 24px;
}
.fj-kpi {
  background: #101010; border: 1px solid #3d3a39; border-radius: 8px;
  padding: 18px 20px; display: flex; flex-direction: column; gap: 8px;
}
.fj-kpi-label {
  font: 600 10.5px/1.4 'JetBrains Mono', monospace;
  letter-spacing: 0.07em; text-transform: uppercase; color: #8b949e;
}
.fj-kpi-value {
  font: 600 28px/1.1 'JetBrains Mono', monospace; color: #f2f2f2;
}
@media (max-width: 1023px) { .fj-kpi-row { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 767px)  { .fj-kpi-row { grid-template-columns: 1fr; } }
```

NOTE for the implementer: before relying on the `#main` / `#content-start`
selectors, open the rendered index in a browser (`../.venv/bin/python manage.py
runserver`, log in with a superuser via
`../.venv/bin/python manage.py issue_login_code --email root@findajob.pro` —
or just `createsuperuser`) and confirm Django 6.0's actual wrapper IDs in the
DOM; adjust the two selectors if the structure differs. The sidebar must sit
flush left, full height, with content flowing beside it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `../.venv/bin/pytest tests/admin/test_admin_dashboard.py -v` → PASS.

- [ ] **Step 6: Visual check**

From `backend/`: run the dev server, open `/admin/`, and verify against
`web/app.html` served locally (`python3 -m http.server` inside `web/`):
sidebar width/colors/icons match, collapse toggle works and persists on
reload, KPI cards match the dashboard card chrome, no horizontal scroll at
375px / 768px / 1280px.

- [ ] **Step 7: Commit**

```bash
git add apps/common/static/admin/fonts apps/common/static/admin/css/voltagent-admin.css \
        tests/admin/test_admin_dashboard.py
git commit -m "feat(admin): Voltagent sidebar + KPI card styling, self-hosted fonts"
```

---

### Task 5: Full-suite verification

- [ ] **Step 1: Run the entire backend suite**

From `backend/`: `../.venv/bin/pytest -q`
Expected: all ~574+ tests pass. If any admin smoke/security test fails on the
new markup, fix the template (not the test) unless the test asserts stock
Django chrome that the theme legitimately replaced — then update the test with
a comment referencing the spec.

- [ ] **Step 2: Commit any fixes**

```bash
git add -A backend/apps/common backend/tests/admin
git commit -m "test(admin): align suite with the Voltagent dashboard chrome"
```

(Skip the commit if nothing changed.)
