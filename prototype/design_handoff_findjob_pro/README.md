# Handoff: Findjob.pro — Job Tracking & AI-Assisted Application Platform

## Overview
Findjob.pro is a multi-tenant SaaS that aggregates job listings from platform-specific modules (RemoteOK, We Work Remotely, Wellfound, LinkedIn, Indeed), scores them against the user's profile, generates AI application content adapted per platform, and manages documents, networking contacts, subscriptions and German-market legal compliance. Target market: Germany (Hamburg-based entity), EU/GDPR. Languages: EN (default), DE, RO.

## About the Design Files
The files in this bundle are **design references created in HTML** (Design Components). They are prototypes showing intended look and behavior — **not production code to copy directly**. The task is to recreate these designs in a **Django 5.x + DRF + HTMX/Alpine.js** stack (per the project architecture prompt), using Django templates with HTMX partial updates. `api.js` and `legal.js` are structured mock data whose shapes intentionally mirror the intended DRF serializers — use them as the serializer/fixture contract.

## Fidelity
**High-fidelity.** Colors, typography, spacing and interactions are final. Recreate pixel-perfectly.

## Files
- `Landing.dc.html` — public marketing page + 3-step onboarding modal (EN/DE/RO)
- `Job Tracking Platform Voltagent.dc.html` — the full authenticated app (dark Voltagent theme, CURRENT design)
- `Job Tracking Platform.dc.html` — earlier light theme (reference only, superseded)
- `api.js` — mock API layer: ENDPOINTS map, PLATFORMS, JOBS, DOCUMENTS, PROFILE, INTEGRATIONS, CONTACTS, PLANS, ORG, USAGE, CANVA_TEMPLATES, generateContent(), suggestDocuments(), skillMatch()
- `legal.js` — 6 legal documents × 3 languages (DE legally binding)

## Design Tokens (Voltagent theme)
- Canvas `#101010` · surface-soft `#1a1a1a` · surface-softer `#2a2a2a`
- Hairline border `#3d3a39` (1px solid everywhere; no drop shadows except modals `0 20px 60px rgba(0,0,0,0.7)`)
- Primary accent `#00d992` (CTAs, active nav, status live) · soft `#2fd6a1` · deep (links) `#10b981` · text-on-primary `#101010`
- Text: ink `#f2f2f2`, strong `#ffffff`, body `#bdbdbd`, mute `#8b949e`
- Semantic: success `#2fd6a1`, warning `#e6b34d`, danger `#e5534b`; tinted fills use rgba, e.g. `rgba(0,217,146,0.10)` bg + `rgba(0,217,146,0.35)` border
- Fonts: **Inter** (400/500/600/700) for UI; **JetBrains Mono** for data, labels, code, metrics
- Radii: buttons 6px, cards 8–10px, pills 9999px. Uppercase eyebrow labels: JetBrains Mono 10.5px, letter-spacing 0.07em, color mute.

## App Screens (authenticated, sidebar nav in this order)
1. **Dashboard** — KPI row (active modules, pending, in review, sent); tabs per platform + "All" (All shows Source column); Table/Board toggle. Table grid columns: `minmax(200px,1.8fr) 132px minmax(140px,1fr) 64px 104px 78px 90px 122px` (Role/Company, Source, platform-specific Details, Match %, Salary, Scraped, Status, Action) inside `min-width:1024px` + `overflow-x:auto` wrapper. Status flow: PENDING → RUNNING (spinner, 3 phases) → REVIEW → SENT. Platform-specific detail column: LinkedIn = Easy Apply/connection, Wellfound = founder name, feeds = apply email. Risk banner per platform tab (high=red tint).
2. **Job Search** (Open-to-Work profile) — job titles (max 5, chips + add), location types (toggle), on-site locations (max 5), remote locations (max 5), start date (radio: immediately/flexible), employment types (toggle), visibility (radio: recruiters only/everyone). Maps to `PUT /api/v1/search-profile/`.
3. **History** — sent applications, filter pills per source, mode AUTO/MANUAL.
4. **Network** — contacts CRM; status pill cycles to_contact → messaged → replied → referred on click.
5. **Documents** — card grid with kind badge + ATS score bar (CVs); Upload (file input) and "Generate with AI" panel: type (CV/cover/brand), target job, 10 Canva templates per type (locked until Canva integration connected).
6. **Profile** — master profile form + skills chips (drives skillMatch and AI prompts).
7. **APIs** — integrations (Anthropic, OpenAI, Canva, Telegram, WhatsApp, Jotform, Slack): API-key input → connect → masked hint; keys encrypted server-side, only hint returned.
8. **Modules** — one card per platform showing the adapter contract: connector_type, auth_method, apply_mechanism, rate_limit + active toggle + risk badge.
9. **Organization** — workspace members, roles owner/admin/member (click cycles admin↔member), invite by email, seats.
10. **Billing** — usage meters (amber >80%), monthly/yearly toggle (~15% off), 4 plans: Free $0, Pro $20/17 (popular), Max $100/85, Team $30/25 per seat min 5. Current plan = 2px green border.
11. **Legal** — doc list + reading pane: terms (AGB), privacy (Datenschutzerklärung), cookies, impressum, withdrawal (Widerrufsbelehrung), dpa (AVV). Full text in `legal.js`, 3 languages, note "DE version is legally binding".

Global: collapsible sidebar (58px icon-only ↔ 218px, chevron toggle, icons are inline SVG paths in the DC logic); EN/DE/RO switcher in header (translates nav, screen titles, cookie banner, legal); TTDSG cookie banner (Accept all / Essential only) on first load; toast notifications bottom-center.

**Review drawer** (right, 560px): skill match % + matched/missing chips; editable message (tone per platform: formal email for feeds/Indeed, short conversational for LinkedIn, personal founder message for Wellfound); editable cover letter (not for LinkedIn/Wellfound); regenerate; toggleable suggested documents; private notes; footer: low-risk platforms → "Approve & send" (auto), high-risk → "Copy & mark as sent" (manual, with warning). LinkedIn warning: automation risks account suspension — user submits manually.

## Landing page
Hero (Inter 56px weight 400, tracking -0.65px, mono eyebrow), terminal mockup card, 6 feature cards, 4-plan pricing, footer with legal links, 3-step onboarding modal: account (email+password+GDPR note) → CV upload (parse into master profile) → preferences (title + location types) → dashboard.

## Django Model Map (from api.js shapes)
- `Platform(id, name, connector_type, auth_method, apply_mechanism, risk, auto_send, is_active, rate_limit_config)`
- `JobListing(platform FK, title, company, url, salary, tags JSON, scraped_at, status, platform_specific_data JSON)`
- `Application(job_listing FK, generated_content, generated_cover_letter, suggested_documents M2M, notes, status, sent_manually, sent_at)`
- `Document(name, kind, tags JSON, file, ats_score)` + `ApplicationDocument`
- `UserProfile(name, headline, email, location, years, tone, skills JSON, summary)`
- `SearchProfile(titles JSON max5, loc_types, loc_onsite JSON max5, loc_remote JSON max5, start, employment_types, visibility)`
- `Integration(provider, api_key encrypted, key_hint, connected)`
- `Contact(name, role, company, source, status, last_touch, next_action)`
- `Organization(name, plan, cycle)` + `Membership(user, org, role)` — multi-tenant: all data scoped by org
- `Subscription(org, plan, cycle, stripe_id)` + `UsageQuota(apps, gens, docs_mb, period_ends)`
- Endpoints: see `ENDPOINTS` in api.js (`/api/v1/...`). AI generation = Anthropic API; prompt includes job description, requirements, user profile, source platform (register/tone selection).

## Production Notes (VPS)
- Stack: Django 5.x + DRF, HTMX + Alpine.js, PostgreSQL, Celery + Redis (one task set per platform module), Playwright only where unavoidable, n8n one workflow per platform.
- i18n: Django gettext for EN/DE/RO; legal docs as versioned DB records seeded from `legal.js`.
- Security: document encryption at rest (AES-256), API keys via `django-fernet-fields` or KMS, TLS 1.3, EU hosting (Hetzner), Stripe for billing, cookie consent gate before any non-essential storage (§ 25 TTDSG).
- LinkedIn/Indeed: never automate submission — semi-manual flows as designed.
