---
name: findajob-architecture
description: The governing architecture for findajob.pro (from main_prompt.md + the prototype handoff) — platform-adapter pattern, per-platform risk rules, data model, status flow, and MVP priorities. Use for ANY backend, model, module, or feature work on findajob, before designing or writing code.
---

# findajob.pro — Architecture (source of truth: `main_prompt.md` + `prototype/api.js`)

> `prototype/design_handoff_findjob_pro/` is a STALE snapshot (missing the Advisor and
> Analytics screens; its `api.js` lacks `SUGGEST`, `careerAnalysis`, `funnelAnalysis`) and its
> README still prescribes Django 5.x. Read `prototype/api.js` — the contract itself.

Multi-tenant SaaS that aggregates jobs from **isolated per-platform modules**, scores them
against the user profile, generates AI application content adapted per platform, and manages
documents, contacts, billing, and German-market legal compliance. Target market: Germany
(Hamburg entity), EU/GDPR. Languages: EN (default), DE, RO.

## Non-negotiable principles

1. **UI-FIRST.** The interface (prototype zip, dark Voltagent theme) is live on findajob.pro
   from day one; the backend wires in underneath it. Never let backend work make the visible
   site regress. (Lesson from the failed 2026-08-01 backend-first attempt.)
2. **Platform Adapter Pattern.** Each source platform = one isolated module exposing the same
   contract. New platforms are added by filling the contract, never by writing bespoke logic
   into the core.
3. **api.js is the API contract.** The prototype's `api.js` mock shapes intentionally mirror
   the intended DRF serializers and `ENDPOINTS` map (`/api/v1/...`). Backend endpoints must
   match those shapes 1:1 so the frontend goes live by swapping mock bodies with `fetch`.
4. **Per-platform risk rules are law** (see table below). Never automate submission on
   high-risk platforms.

## Target stack

**Django 6.0 + DRF** (decided 2026-08-01; pin `Django>=6.0,<6.2`) · HTMX + Alpine.js (or keep
the prototype's static shell until replaced) · PostgreSQL · Celery + Redis (one task set per
platform module) · Playwright only where unavoidable · n8n = one workflow per platform
(PERSONAL account, app.n8n-studio.com) · Anthropic API for generation · document storage
local/R2, encrypted at rest.

**Why 6.0 and not 5.2 LTS:** 6.0 ships natively what this project would otherwise backport as
three extra dependencies — template partials (the HTMX fragment story), the Tasks framework,
and native CSP. Upgrade path: 6.1 (Aug 2026, minor bump) → **6.2 LTS (Apr 2027) is the
long-term home**. 6.0 gets security fixes until Apr 2027, so the window is covered.

**Django 6.0 rules that bite** (see the `django-6-senior` skill for the full list):
- `send_mail()` / `get_connection()` / `mail_admins()` optional args are **keyword-only**;
  `EmailMessage.message()` returns the modern `email.message.EmailMessage`.
- `CheckConstraint(condition=...)` — `check=` was removed.
- `DEFAULT_AUTO_FIELD` defaults to `BigAutoField`; custom ORM expressions must return params
  as **tuples**; `Field.pre_save()` may be called more than once per save (keep it idempotent).
- Template partials: `{% partialdef name %}` + render directly as `"template.html#name"`.
- Tasks framework = contract only, **no worker**; bundled backends (`ImmediateBackend`,
  `DummyBackend`) are dev/test only. Task args must survive a JSON round-trip.

## Adapter contract (every platform module)

| Field | Values |
|---|---|
| `connector_type` | api / rss / scraper_html / browser_automation |
| `auth_method` | none / api_key / cookie_session / login_form |
| `apply_mechanism` | email / external_redirect / in_platform_form / message_to_founder |
| `rate_limit` | per-module config (delay, daily quotas) |
| `field_mapping` | raw fields → normalized `JobListing` |

## Per-platform rules

| Platform | Connector | Risk | Rule |
|---|---|---|---|
| LinkedIn | browser automation | HIGH (account ban) | Semi-manual only. AI generates a short conversational message; the USER clicks submit. **Never automate Easy Apply.** |
| Indeed | official feed or manual paste | HIGH (legal anti-scraping) | Never scrape. Feed or copy-paste input only. |
| Wellfound | limited API / session scraping | MEDIUM | Semi-manual; AI writes a personal founder message, mandatory review. |
| WWR / RemoteOK / Himalayas / Remotive | official RSS/JSON | LOW | Full automation OK (scrape + generate + auto-send email). |
| Arc, JustRemote, Contra, NoDesk, etc. | generic RSS template | LOW | Clone of the RSS module, adapted field_mapping. |

AI tone per platform: feeds/Indeed → formal email + cover letter; LinkedIn → short
conversational note (no cover letter); Wellfound → personal founder message (no cover
letter). Prompt includes job description, requirements, user profile, source platform.
**Manual review step is mandatory for all platforms before send.**

## Status flow

`PENDING → RUNNING (3 phases, spinner) → REVIEW → SENT` (+ interview/offer downstream).
On low-risk platforms SENT is set automatically; on LinkedIn/Wellfound/Indeed the user marks
it manually after actually sending ("Copy & mark as sent").

## Data model (scope everything by org — multi-tenant)

- `Platform(id, name, connector_type, auth_method, apply_mechanism, risk, auto_send, is_active, rate_limit_config)`
- `JobListing(platform FK, title, company, url, salary, tags JSON, scraped_at, status, platform_specific_data JSON)` — platform-specific data (e.g. `easy_apply`, founder name, apply email) lives in the JSON field
- `Application(job_listing FK, generated_content, generated_cover_letter, suggested_documents M2M, notes, status, sent_manually, sent_at)`
- `Document(name, kind, tags JSON, file, ats_score)` + `ApplicationDocument`
- `UserProfile(name, headline, email, location, years, tone, skills JSON, summary)` — drives skillMatch + AI prompts; consider JSON Resume schema
- `SearchProfile(titles JSON max5, loc_types, loc_onsite JSON max5, loc_remote JSON max5, start, employment_types, visibility)`
- `Integration(provider, api_key encrypted, key_hint, connected)` — providers: Anthropic, OpenAI, Canva, Telegram, WhatsApp, Jotform, Slack; only the hint is ever returned
- `Contact(name, role, company, source, status, last_touch, next_action)` — status cycles to_contact → messaged → replied → referred
- `Organization(name, plan, cycle)` + `Membership(user, org, role owner/admin/member)`
- `Subscription(org, plan, cycle, stripe_id)` + `UsageQuota(apps, gens, docs_mb, period_ends)` — plans: Free $0, Pro $20/17, Max $100/85, Team $30/25 per seat min 5
- Legal docs = versioned DB records seeded from `legal.js` (6 docs × EN/DE/RO; **DE is legally binding**)

## Contract gaps to close in the backend phase (audited 2026-08-01, each verified in the code)

`api.js`'s `ENDPOINTS` map has **11 entries** but the UI drives far more. Treat the list below
as the backlog — the mock lets the prototype "work" while hiding the missing write paths.

**Missing endpoints**
- **Auth: nothing at all.** Onboarding collects email + password (`index.html`) into local
  state and then navigates with a plain `<a href="app.html">`. No register / login / logout /
  session / password-reset / `/me/` anywhere. Multi-tenancy is undefined until this exists.
- No endpoint for 5 screens the UI already labels: `GET/PUT /search-profile/`, `GET /advisor/`,
  `GET /analytics/funnel/`, `GET /applications/?status=sent` (History), `GET /legal/?lang=`.
- **AI document generation has no endpoint** — the headline feature. `runGenerate` fabricates a
  Document client-side and invents `ats_score: 95`. Nothing computes an ATS score.
- `GET /suggest/?type=&q=` (Job Search typeahead) and
  `GET /integrations/canva/templates/?type=` are consumed live but absent from `ENDPOINTS`.
- Billing has only `subscribe`. No cancel, seats, invoices, or Stripe webhook receiver — yet
  the shipped AGB promises cancellation to period end and a 14-day withdrawal refund.
- Six user-mutable affordances write nowhere: application stage advance (which the whole
  Analytics funnel and Advisor conversion score are computed from), private notes, org role
  change, member removal, notification prefs, GDPR export/deletion.

**Wrong shapes**
- `POST /applications/:id/send/` accepts only `{sent_manually}` — so **every edit made in the
  Review drawer** (message, cover letter, attachments, notes) is silently discarded on send.
  It must accept the edited payload.
- Field names diverge from this model map: `psd` vs `platform_specific_data`, `rate_limit` vs
  `rate_limit_config`. Pick one spelling and make the serializer match.
- `status` is declared on **both** `JobListing` and `Application`, with no rule for which the
  Dashboard reads, and the mock returns application-scoped fields (`sent_manually`) flat on a
  job row. A scraped job is shared; an application is per-owner — keep the state on
  `Application`.
- `JobListing` is missing `description`, `requirements` and `location`. All three are rendered
  by the UI and required by the AI prompt spec.

**Algorithm bugs to fix during the port (not to reproduce faithfully)**
- `skillMatch` divides by `job.tags.length` with no zero guard → a scraped job with no tags
  renders `NaN%`. Guard it, and compute server-side: it currently runs per row per render.
- The Advisor's "activity" score divides an **all-time** sent count by a **weekly** baseline of
  10, so the number is meaningless. `api.js` calls itself the spec for the Python port — fix
  the window before porting.

## Security & compliance

Documents encrypted at rest (AES-256); API keys encrypted server-side (Fernet/KMS); TLS;
EU hosting; TTDSG cookie consent gate before any non-essential storage; GDPR: no public
indexing of personal documents.

## MVP priorities (from main_prompt.md §13)

1 modular multi-source aggregation · 5 AI CV tailoring per job · 8 multi-version CV RO/EN/DE ·
12 tone per platform · 15 auto-fill without auto-submit on risky platforms ·
18 daily/weekly per-platform limits · 20 funnel analytics. Everything else is round 2.

## Reference repos (patterns, not code to run)

JSON Resume (UserProfile schema) · chatgpt-cover-letter-generator (extract → draft → refine →
consolidate pipeline) · ats-screener (in-browser ATS scoring, privacy model) · AIHawk
(architecture reference ONLY — never its LinkedIn auto-submit).
