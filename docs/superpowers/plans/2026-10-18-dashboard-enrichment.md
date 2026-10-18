# Dashboard Enrichment (Dark Theme + Matched Jobs + Automation) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Turn the Plan-1 command-center shell into a usable dark-themed dashboard: a dark (Voltagent) theme with a light toggle, a Documents summary, a Recommendations panel, a main **nomad-jobs table with match %**, and an internal **job-ingestion automation table** — porting the deterministic matcher from `Job_app`.

**Architecture:** Server-rendered Django (`apps.dashboard`, `/app/`) as in Plan 1. Add `apps.profiles` (`UserProfile`, `SearchProfile`) and `apps/jobs/matching.py` (ported from `Job_app/filters/relevance.py`). The dark theme is a CSS layer using the Voltagent tokens applied over the vendored Jobick structure, default-on via `data-theme="dark"`, toggled client-side with `localStorage`. `build_command_center` grows new keys; `index.html` grows four blocks.

**Tech Stack:** Django 6.0, Django templates, Bootstrap 5 (Jobick) + Voltagent dark CSS, pytest.

**Spec:** `docs/superpowers/specs/2026-10-18-jobick-dashboard-design.md` (§8b), grounded in `docs/superpowers/specs/2026-09-26-pipeline-port-design.md` (§4,§5,§10). Design system: the `voltagent-design` skill.

## Global Constraints

- Django 6.0; no third-party browser origin (dark CSS + any fonts vendored locally, no CDN).
- Org-scoped reads scoped via `.for_org`/`.for_user`; unscoped raises `OrgScopeError`.
- Dark = Voltagent tokens (canvas `#101010`, hairline `#3d3a39`, accent `#00d992` for CTA/status ONLY, Inter + JetBrains Mono). Light = Jobick default. SSR default = **dark**; no light-flash.
- Matcher ported **1:1** from `Job_app/filters/relevance.py` with its tests (`Job_app/tests/test_relevance.py` if present); behavior must match.
- `Job_app` (`~/Projects/Job_app`) is inspiration — port pure functions + tests, re-express UI.
- Every UI task MUST invoke `voltagent-design` and `ui-ux-pro-max` before writing templates/CSS.
- pytest from `backend/` on `config.settings.test`. No heavy new deps (matcher is pure Python).

## Review Focus

- **Theme default + persistence:** first paint is dark with no light flash; toggle persists across reloads; a viewer who chose light stays light. (Task 1)
- **Empty profile / no skills:** match % renders a neutral value (e.g. "—" or 0), never a crash or a divide-by-zero. (Tasks 2,4)
- **Empty data blocks:** no documents / no jobs / no ingestion history each render an honest empty state, not broken markup. (Task 5)
- **Cross-tenant:** the jobs table, recommendations, documents, and match all scope to the viewer's org/user; no other tenant's rows leak. (Task 4)
- **Matcher parity:** ported `prescore`/`skill_match` produce the same scores as `Job_app` on the ported test cases. (Task 2)

---

### Task 1: Dark theme (Voltagent) default + light toggle, persisted

**Files:** Modify `backend/apps/dashboard/templates/dashboard_app/base.html`; Create `backend/apps/dashboard/static/dashboard_app/css/theme-voltagent.css`; Test `backend/tests/dashboard/test_theme.py`.

**Interfaces — Produces:** `<html data-theme="dark">` default; a topbar toggle (`#theme-toggle`) that flips `data-theme` dark↔light and writes `localStorage['fj-theme']`; an inline head script that reads `localStorage` before first paint (no flash).

- [ ] Invoke `voltagent-design` + `ui-ux-pro-max`; build the dark palette from the Voltagent tokens.
- [ ] Write `test_theme.py::test_default_theme_is_dark` — GET `/app/` (logged in) → HTML has `data-theme="dark"` on the root and a `#theme-toggle` control.
- [ ] Run it, expect fail.
- [ ] Implement: `theme-voltagent.css` restyles the Jobick shell surfaces to Voltagent dark under `[data-theme="dark"]` (canvas, cards, hairlines, text, accent for active nav/buttons/status only); `base.html` sets `data-theme="dark"` by default, adds the toggle + the no-flash inline script (reads `localStorage['fj-theme']`, default dark). Light = the stock Jobick CSS when `data-theme="light"`. Keep all assets local.
- [ ] Run to green; commit.

### Task 2: Port the deterministic matcher from Job_app

**Files:** Create `backend/apps/jobs/matching.py`; Test `backend/tests/jobs/test_matching.py`.

**Interfaces — Produces:** `skill_tokens(skill: str) -> list[str]`; `prescore(job: dict, config: dict, search_keywords=None) -> tuple[int, list[str]]`; `skill_match(job_listing, search_profile) -> dict` = `{"score": int, "matched": list[str], "missing": list[str]}` (tokenize over title+description+tags+requirements; zero-guard an empty profile → `{"score": 0, "matched": [], "missing": [...]}`, never divide-by-zero).

- [ ] Read `~/Projects/Job_app/filters/relevance.py` and `~/Projects/Job_app/tests/test_relevance.py` (if present). Port `skill_tokens`, `prescore` (and helpers `_role_head`/`_contains`/`_matches`/`_market_keywords` as needed) verbatim-in-behavior into `matching.py`.
- [ ] Write `test_matching.py` porting the Job_app relevance cases 1:1 (same inputs → same scores), plus a `skill_match` test over a real `JobListing` + a `SearchProfile`-shaped config, plus the empty-profile zero-guard case.
- [ ] Run (expect fail), implement, run to green; commit.

### Task 3: `apps.profiles` — UserProfile + SearchProfile

**Files:** Create `backend/apps/profiles/{__init__,apps,models,migrations/__init__}.py`; Modify `backend/config/settings/base.py` (`LOCAL_APPS += "apps.profiles"`); Test `backend/tests/profiles/test_models.py`.

**Interfaces — Produces:** `SearchProfile` (org+user unique; `skills_primary`/`skills_secondary`/`negative_keywords`/`exclude_keywords`/`flex_signals` JSON lists, `keyword_groups` JSON list[list], `min_prescore` int=30) with `to_matching_config() -> dict` in the shape `prescore()` expects; `UserProfile` (org+user unique; fields per pipeline-port §4). Both org-scoped via `OrgScopedManager(user_field="user")` + `all_objects`. A `get_or_create_search_profile(org, user) -> SearchProfile` helper (lazy, empty defaults) — NOT created in `bootstrap_workspace`.

- [ ] Fields + `to_matching_config()` per pipeline-port §4/§5. Write `test_models.py`: `to_matching_config()` returns the dict `prescore()` consumes; org-scoping raises on unscoped eval; lazy create.
- [ ] Run (fail), implement, makemigrations, run to green; commit.

### Task 4: Enriched `build_command_center` context

**Files:** Modify `backend/apps/dashboard/kpis.py`; Create `backend/apps/dashboard/automation.py`; Test `backend/tests/dashboard/test_enriched_context.py`.

**Interfaces — Consumes:** `skill_match` (Task 2), `get_or_create_search_profile` (Task 3), `resolve_org` (Plan 1). **Produces** new `cc` keys: `documents_summary` (list of `{kind, label, count}` from `Document.objects.for_org(org).for_user(user)`); `job_rows` (latest N JobListing each annotated `{listing, match}` where `match = skill_match(listing, profile)`, sorted by match score desc); `recommendations` (top 5 of `job_rows` by score, score ≥ profile.min_prescore); `ingestion_runs` (from `ingestion_runs(limit)` in `automation.py`: per `Platform`, `{platform, last_scraped_at, new_count}` derived from `JobListing` — latest `scraped_at` + count in the last 7 days). `org is None` → all empty, no raise.

- [ ] Write `test_enriched_context.py`: job_rows carry a `match` with a numeric score; recommendations only include score ≥ min_prescore; documents_summary counts by kind; ingestion_runs groups by platform with a count; empty-workspace path returns empties without raising; a second user's rows never appear.
- [ ] Run (fail), implement (`ingestion_runs()` in `automation.py`; extend `build_command_center`), run to green; commit.

### Task 5: Enriched `index.html` — four blocks, Voltagent dark

**Files:** Modify `backend/apps/dashboard/templates/dashboard_app/index.html`; Test `backend/tests/dashboard/test_enriched_index.py`.

**Interfaces — Consumes:** the `cc` keys from Task 4.

- [ ] Invoke `voltagent-design` + `ui-ux-pro-max`. Render, in Voltagent dark chrome: (1) **Documents** summary card (kind + count, empty state "No documents yet"); (2) **Recommendations** panel (top matched jobs, score pill); (3) the **main jobs table** — columns Role/Company · Platform · Match % · Salary · Status · Action — with a match-score pill (green tint ≥ min_prescore) and an `overflow-x:auto` wrapper per the design system's table rules; (4) an **Automation** table — ingestion runs (Platform · Last run · New jobs). Honest empty states throughout.
- [ ] Write `test_enriched_index.py`: GET `/app/` (logged in, seeded) → 200; the jobs table shows a seeded job title + its match % and status; the documents card shows a seeded kind/count; the automation table shows a platform row; an empty workspace shows the empty states. Run first (fail), implement, run to green, then the FULL suite; commit.

---

## Self-Review

- **Spec coverage (§8b):** dark-default+toggle → Task 1; matcher port → Task 2; profile → Task 3; documents + recommendations + jobs-table-with-match% + ingestion automation table → Tasks 4,5; Job_app ported as functions+tests → Task 2. All covered.
- **Review Focus:** theme no-flash/persist (T1), empty-profile neutral match (T2/T4), empty blocks (T5), cross-tenant (T4), matcher parity (T2) — each owned.
- **Type consistency:** `skill_match` signature fixed in T2, consumed in T4; `cc` keys fixed in T4, consumed in T5; `SearchProfile.to_matching_config()` in T3 feeds T2's `prescore`.
- **Proportion:** plan carries the decisions (new app/models, matcher source, `cc` keys, theme mechanism, table columns); implementers write bodies and the Voltagent CSS.
