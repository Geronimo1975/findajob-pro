# Dashboard, CRM and Documents — design spec

**Date:** 2026-09-26 · **Status:** reviewed draft, awaiting operator approval · **Sub-project 1 of 3**

This is the first of three sub-projects that turn findajob.pro from a demo shell into the
product a paying user gets. The order was set by the operator on 2026-09-26:

1. **This spec:** the app shell rewritten as static HTML, a dashboard on real data, document
   upload, and a networking CRM.
2. Next: the application pipeline (generate → review → send), application progress tracking.
3. Then: apply-by-link ingestion and Stripe billing.

Everything here builds on what already ships and is verified in `backend/`: OTP login,
`GET /api/v1/me/`, `Organization`/`Membership` tenancy, `OrgScopedManager`, the `Document`,
`Application` and `ApplicationDocument` models, `GET /platforms/` and `GET /jobs/`, and the
SMTP `MailboxConnection`. It does **not** touch generation, sending, scraping or billing.

Precedence: `docs/2026-08-01-auth-implementation-spec.md` governs auth and the error
envelope; `docs/2026-08-02-outbox-spec.md` governs the mailbox;
`docs/superpowers/specs/2026-08-02-verify-platform-design.md` governs `verify_platform`.
Where this spec and `web/api.js`'s fixtures disagree on a wire shape, this spec wins and the
fixture is updated. Where this spec and CLAUDE.md disagree on the app-shell boot behaviour
(§2.2), the shipped code and this spec win and CLAUDE.md is corrected in the same commit.

This draft was reviewed by six independent lenses (backend, frontend/nginx, security and
tenancy, design system, spec quality, testing/deploy) with three-vote adversarial
verification per finding against the real codebase; every confirmed finding is folded in
below, and the three refuted ones (sidebar contract, language fallback, in-use rule for
finished applications) are left as originally written.

---

## 1. Goals and non-goals

**Goals**

- A signed-in member opens `/app.html` and sees **their own** data: counts, a funnel, their
  workspace's documents and contacts, the ingested job table. A brand-new account sees an
  honest empty state, never sample data.
- Members can upload, rename, retag, download and delete documents.
- Members can keep a networking CRM: add contacts, edit them, move them through
  `to_contact → messaged → replied → referred`, record the next action.
- The anonymous demo (`?demo=1`, an anonymous visit, or an unreachable API) keeps working on
  fixtures, exactly as today. UI-first: nothing visible regresses.
- The frontend becomes maintainable: plain HTML, CSS and ES modules, no visual-editor
  export, no generated React runtime, no `new Function`.
- The privacy policy stays true: documents are encrypted at rest (§4.1), as `web/legal.js`
  §7 already promises in all three languages.

**Non-goals (explicitly later)**

- AI generation, ATS scoring, `POST /jobs/:id/generate/`, `POST /applications/:id/send/`.
- Application stage advance from the UI, History screen on real data.
- Apply-by-link, on-demand scraping, new adapters.
- Stripe, plans, quotas beyond the storage cap below.
- Search profile, Profile, Integrations, Organization, Billing on real data. These screens
  are ported to the new shell **on their current fixtures** so the 13-screen product stays
  visible.
- Encryption key rotation (the file format reserves room for it, §4.1).

---

## 2. Frontend: the static app shell

### 2.1 Files

| File | Role |
|---|---|
| `web/app.html` | Rewritten. Plain HTML: `<head>` with fonts + `app.css`, the sidebar, a top bar, one `<main id="screen">`, `<script type="module" src="app.js">`. No `support.js`, no React, no `window.__resources`, no banner markup (the banner is mounted by `consent-banner.js`, §2.2). |
| `web/app.css` | All app chrome as real CSS classes built from the Voltagent tokens (§2.4). Responsive rules are ordinary media queries at `max-width: 899px` and `max-width: 560px`; no `!important`, no attribute selectors, because there are no inline styles to beat. |
| `web/app.js` | Boot (§2.2), router, sidebar, toast, drawer, the `ctx` object handed to screens. Exposes nothing global. |
| `web/ui.js` | Shared screen helpers with no DOM state and no HTTP: `formatDate`, `formatBytes`, `el()`/`html` builders, `escape`, the status-pill map (§2.4). Imported by `app.js` and every screen. |
| `web/i18n.js` | One `STRINGS` object, `en`/`de`/`ro`, every user-visible string of the app, plus `STRINGS.<lang>.errors` (below). `t(key, vars)` helper. Reads/writes the same localStorage language key `index.html` and `consent-banner.js` use (`findajob.lang`). |
| `web/screens/<id>.js` | One module per screen, default export `render(root, ctx)`; `ctx` carries `api`, `session`, `t`, `toast`, `drawer`, `navigate`. The thirteen ids are **the existing nav ids of `app.html`**, unchanged so GA `page_path` (`/app/<id>`) stays continuous: `dashboard`, `search`, `advisor`, `analytics`, `history`, `network`, `documents`, `profile`, `integrations`, `settings`, `org`, `billing`, `legal`. `search` is the Job Search Profile screen (fixtures, a non-goal); the job table lives in `dashboard`. |
| `web/api.js` | Stays the single owner of HTTP. Gains the functions in §6 and the `liveApi`/`createDemoApi()` split (§2.3). Fixtures updated to the new wire shapes. |

**Error strings.** `STRINGS.<lang>.errors` maps every machine code the app can receive to
one sentence, with `generic` as the fallback — the same table shape as `auth.html`'s
`errors`. Required keys in all three languages: the transport and framework codes
`network_unreachable`, `http_error`, `csrf_failed`, `not_authenticated`, `not_org_member`,
`permission_denied`, `not_found`, `rate_limited` (takes `{s}` from `err.retryAfter`),
`server_error`, `service_unavailable`, `unsupported_media_type`; the codes this spec
introduces: `unsupported_file_type`, `file_too_large`, `storage_quota_exceeded`,
`document_in_use`, `already_final`, `invalid_status`, `invalid_kind`, `invalid`; and
`generic`. `ctx.toast.error(err)` renders `t('errors.' + (err instanceof ApiError ?
err.code : 'generic'))`; the server's English `detail` is never shown. `invalid` (400 with
`fields`) is not toasted: each key in `error.fields` puts its message under the matching
form input in danger colour and the first invalid field receives focus. `not_authenticated`
(a session that expired mid-use) is not toasted either: the shell navigates to
`/auth.html?next=%2Fapp.html%23%2F<screen>`. The three notices get keys too:
`toast.coming_next`, `toast.demo_only`, `notice.backend_unreachable`.
`test_project_contract.py` gains a check that every literal in `ErrorCode` plus the codes
above has an `errors.*` key in each language (parsed with `apps.legal.jsparse`, no Node).

**Removals.** `support.js` and `web/vendor/react*.js` are deleted once `app.html` no longer
references them (`index.html`, `auth.html` and `unternehmen.html` already do not; verified by
grep). The CI guard `webroot carries only product files` (cap 40 tracked files under `web/`)
is raised to 60 in the commit that adds `web/screens/`; the port lands at 36.

**Cache policy (nginx).** Everything that shares `app.html`'s release cycle shares its
`expires -1` policy, for the reason already written above the existing rule in
`deploy/nginx.conf`: otherwise a deploy leaves a browser running new `app.html` against
hour-old CSS or modules. The existing alternation `^/(api|support|legal)\.js$` cannot express
`screens/`, so the block becomes, verbatim, and must stay above the generic 1h asset rule:

```
location ~* ^/(api|app|i18n|ui|legal|support|consent-banner|analytics|screens/[^/]+)\.js$|^/app\.css$ {
    expires -1;
    try_files $uri =404;
}
```

It ships as two nginx commits (§9): **nginx A**, deployed *before* the frontend, adds the
new names while keeping `support` and `'unsafe-eval'` (harmless while the files do not exist:
`try_files … =404`); **nginx B**, deployed *after* the frontend, removes `support` from the
alternation and drops `'unsafe-eval'` from `script-src`. `vendor/` and `fonts/` keep the 1h rule.

### 2.2 Boot sequence

This restates the shipped contract in `web/app.html` (its comment block above the `/me/`
call is the authority; the CLAUDE.md sentence "on 401 it redirects to `/auth.html`" is stale
and is corrected in this sub-project).

1. **Consent.** `app.js` calls `mountConsentBanner()` from `consent-banner.js` on boot; there
   is no banner markup in `app.html`. The module's docstring ("app.html does NOT use this
   module") is updated. `analytics.js` is imported the same way `index.html` does it.
2. **Language.** localStorage `findajob.lang` → `navigator.language` → `en`. (`index.html`
   writes the key on every load, so a visitor arriving from the German landing gets German.)
3. **Session.** If `?demo=1`, or if the hash is a legal route (`#/legal/<doc>` or the legacy
   `#legal-<doc>`), skip the network entirely and render with `session = null` — § 5 DDG
   requires the Impressum to be reachable without an account, and every public page links to
   `app.html#legal-<doc>`. Otherwise call `GET /api/v1/me/`:
   - `200` → real mode, `ctx.api = liveApi`.
   - `401` → **demo mode with `session = null`** (today's behaviour: the anonymous visitor is
     never redirected; the sidebar identity control reads "Sign in" and links to
     `/auth.html?next=%2Fapp.html`).
   - network error / 5xx → demo mode plus the persistent `notice.backend_unreachable` strip
     under the top bar (§2.4).
4. **Router.** Hash routes `#/<id>` for the thirteen ids and `#/legal/<doc>`; default
   `#/dashboard`. The router also accepts the legacy `#legal-<doc>` spelling as an alias of
   `#/legal/<doc>` — it is published on `index.html`, `auth.html`, `unternehmen.html` and in
   `consent-banner.js` and those links are **not** rewritten. The router listens to
   `hashchange`, so the banner's Cookie Policy link works from inside `app.html` without a
   reload. `trackPageView` keeps the `/app/<id>` `page_path`.
5. **Sidebar.** Collapsible 58px ↔ 218px as today: initial state
   `matchMedia("(max-width: 899px)").matches`, a toggle button flips
   `.shell[data-sidebar="collapsed"]` and remembers the choice in localStorage (try/catch);
   nav buttons keep `title` and `aria-label` so the icon-only state is usable; the mono API
   hint in the header is hidden under 900px.

### 2.3 Real mode vs demo mode

`USE_MOCK` and its off-localhost throw guard in `api.js` are untouched (auth spec §9, mocks
fail closed). Demo mode is a different mechanism: `api.js` exports `liveApi` (the
`request()`-backed functions of §6) and `createDemoApi()`, which returns an object with the
**same function names** resolved from an in-memory copy of the fixtures, so writes mutate
only that copy. `app.js` sets `ctx.api = session ? liveApi : createDemoApi()` after the
session check; screens never import fixtures directly and never branch on mode.

The fixtures are updated so that **the render code is identical in both modes**: dates are
ISO strings and sizes are bytes, formatted by `ui.js`, never pre-formatted in data.
`careerAnalysis`/`funnelAnalysis` format nothing and are unaffected. Fixture personas remain
anonymous-demo only.

Once `session` is non-null the page **never renders fixtures**. A screen whose `ctx.api.*`
call rejects renders the screen's error card (eyebrow "Unavailable", the mapped error
string, a Retry button). A 404 `not_found` from a list endpoint is treated the same — it
means an older backend (the deploy window in §9) — not as an empty state. Only the `/me/`
call can put the shell into demo mode.

Write operations in demo mode (upload, add contact, advance) succeed against the in-memory
copy and show `toast.demo_only`. Nothing is persisted.

### 2.4 Design

Tokens, chrome and rules come from the `voltagent-design` skill and are not restated. The
port is pixel-faithful to the current `app.html` screens with the exceptions fixed below;
this spec changes content, not looks. Every new element reuses chrome that already ships in
`app.html` or the skill; nothing else is invented.

- **Empty state** (Dashboard, Documents, Network): card chrome; eyebrow label, one sentence
  in body colour, one ghost CTA using the job table's existing empty-row button
  (`color: #2fd6a1; border: 1px solid rgba(0,217,146,0.35); border-radius: 6px;
  padding: 8px 16px`, hover bg `rgba(0,217,146,0.10)`).
- **Meter** (`.meter`): the Review drawer's existing 6px bar — track `#2a2a2a`, radius 3px,
  `overflow: hidden`; fill same radius.
- **Storage card** (Dashboard, Documents): `used / limit` in JetBrains Mono via
  `formatBytes`, then a `.meter` with fill `#00d992` below 80 %, `#e6b34d` at ≥80 %,
  `#e5534b` at ≥95 % of `limit_bytes`.
- **Next actions card** (Dashboard): card chrome; rows separated by a 1px hairline; contact
  name Inter 600 ink, company mute, `next_action` body, `next_action_at` JetBrains Mono 11px
  right-aligned, in `#e6b34d` when before today. Each row is an `<a href="#/network">`.
- **Backend-unreachable notice**: full-width strip under the top bar, warning tint pair
  (`rgba(230,179,77,0.10)` / `rgba(230,179,77,0.35)`), mono eyebrow, one sentence, no
  dismiss (it clears on reload).
- **Drop zone**: the Documents grid container itself; on `dragover` it takes the skill's
  active-card border `rgba(0,217,146,0.5)` and a mono eyebrow "Drop to upload". No dashed
  border, no fill.
- **Upload progress**: a temporary card in the grid with a `.meter` and a mono `NN %` label;
  on error the card takes the danger tint pair, the mapped error string and a Retry ghost
  button.
- **Destructive button** (`.btn-danger`): bg `rgba(229,83,75,0.12)`, border
  `rgba(229,83,75,0.35)`, text `#e5534b`, same shape as `.btn-outline`. Used only in the
  delete-confirm drawer footer.
- **Filter pills** (Network): the skill's pill chrome; selected = green tinted pair + text
  `#2fd6a1`. Each pill is a `<button>` with `min-height: 32px` and enough padding for a 44px
  hit area.
- **Search box**: the input chrome, `max-width: 280px`, placeholder in mute.
- **Status pills**: `ui.js` carries one map for all nine `ApplicationStatus` values:
  `pending` mute, `running` and `sending` spinner, `review` warning tint, `sent` green tint,
  `interview` green tint, `offer` 2px primary border, `rejected` mute strikethrough,
  `failed` danger tint. Contact statuses: `to_contact` mute, `messaged` warning tint,
  `replied` green tint, `referred` 2px primary border.
- **Tables**: every data table is `<div class="table-wrap">` (card chrome: `#101010`, 1px
  hairline, radius 10px, `overflow-x: auto; max-width: 100%; min-width: 0`) wrapping one
  `<table>` with `table-layout: fixed; width: 100%; border-collapse: collapse` and a
  `min-width` that makes the wrapper — never the page — the scroller: `1024px` for the jobs
  table, `720px` for contacts and history. Column widths go in a `<colgroup>`: fixed columns
  carry the skill's pixel widths, flexible columns share the remainder. Cell padding
  `13px 6px`, first cell `padding-left: 18px`, last cell `padding-right: 18px`; `<thead>`
  cells are the mono eyebrow on `#1a1a1a` with the `#3d3a39` bottom border; body rows
  `border-bottom: 1px solid #2a2a2a`, `font-size: 13.5px`.
- **Touch targets**: `.btn`, `.btn-primary`, `.btn-outline`, `.btn-danger`, sidebar nav rows
  and the language toggle get `min-height: 44px` (the skill rule wins over pixel-faithfulness;
  `consent-banner.js` already does this). Non-interactive status pills and tag chips are
  `<span>`s and exempt.
- **Focus**: `app.css` sets `:focus-visible { outline: 2px solid #00d992; outline-offset:
  -1px; }` globally (same rule as `index.html`) and nothing styles bare `:focus`.
- **Accessibility floor**: every interactive element is a `<button>` or `<a>`; the drawer
  traps focus, closes on Escape and returns focus to its opener; tables use real `<table>`
  markup; the upload input is labelled; rows are never click targets (§5.3).

---

## 3. Dashboard on real data

### 3.1 `GET /api/v1/dashboard/`

Session + org (`IsOrgMember`). One round-trip so the screen renders in one paint.

```json
{
  "counts": {
    "jobs_new": 12,          // active-platform listings FIRST SEEN in the last 7 days (created_at)
    "jobs_pending": 148,     // active-platform listings the member has no Application row for
    "applications": {        // the member's own, by status; all nine keys always present
      "pending": 0, "running": 0, "review": 2, "sending": 0, "sent": 5,
      "failed": 0, "interview": 1, "offer": 0, "rejected": 1
    },
    "documents": 4,          // org-wide
    "contacts": 7            // org-wide
  },
  "funnel": [                // ordered, cumulative; the member's own applications
    {"stage": "review",    "count": 9},
    {"stage": "sent",      "count": 7},
    {"stage": "interview", "count": 2},
    {"stage": "offer",     "count": 0}
  ],
  "storage": {"used_bytes": 1843200, "limit_bytes": 52428800},   // org-wide
  "recent_applications": [   // the member's 5 most recently updated, any status
    {"id": 31, "job": {"id": 9, "title": "…", "company": "…", "platform": "remoteok"},
     "status": "sent", "updated_at": "2026-09-24T10:12:00Z"}
  ],
  "next_actions": [          // org-wide contacts with next_action_at set; overdue first, then upcoming ascending; max 5
    {"id": 4, "name": "…", "company": "…", "next_action": "…", "next_action_at": "2026-09-28",
     "overdue": false}
  ]
}
```

Rules:

- **Scope per field.** `jobs_new` and `jobs_pending` are computed on the global listing pool
  (listings are shared). `applications`, `funnel` and `recent_applications` are the member's
  own rows: `Application.objects.for_org(org).filter(user=request.user)`. `documents`,
  `contacts`, `storage.used_bytes` and `next_actions` are **org-wide**:
  `Document.objects.for_org(org)` / `Contact.objects.for_org(org)` with no user filter, so
  the tiles agree with what the Documents and Network screens list (§4.2, §5.2) and with the
  per-org quota (§4.1).
- **`jobs_new`** uses `created_at`, not `scraped_at`: ingestion is an `update_or_create`
  that refreshes `scraped_at` on every run, so `scraped_at` would count the whole catalog.
- **Funnel** is cumulative and defined once, in `apps/jobs/services_dashboard.py::funnel_for(membership)`,
  which the Analytics screen reuses in sub-project 2: `review` = `review` + `sending` +
  everything downstream; `sent` = `sent` + `interview` + `offer` + `rejected`; `interview` =
  `interview` + `offer`; `offer` = `offer`. `failed` never counts as sent (the employer did
  not receive it) and is excluded from the funnel; it surfaces only in
  `counts.applications.failed`. `pending` and `running` are pre-funnel.
- **`storage_for(org)`** is `Document.objects.for_org(org).aggregate(Sum("size_bytes"))` or 0
  and `settings.DOCUMENT_STORAGE_LIMIT_BYTES` (default 50 MiB until plans exist). It is the
  single implementation used by the dashboard, the Documents-screen storage card (which reads
  `GET /dashboard/`, no separate endpoint) and the upload quota check (§4.1).
- **`next_actions`** includes overdue rows (`next_action_at < today`, UTC) first, then
  upcoming, ascending; `overdue` is computed server-side.

### 3.2 Screen

The current Dashboard layout stays: stat tiles (Jobs pending, In review, Applications sent,
Interview rate, Active modules, Auto vs manual), the job table from `GET /jobs/`, and the
Review drawer. Changes:

- Tiles read `counts`: "Jobs pending" = `counts.jobs_pending` (not
  `counts.applications.pending`, which is the count of *application rows* in `pending`);
  "In review" = `applications.review`; "Applications sent" = funnel `sent`; "Interview rate"
  = funnel `interview` ÷ funnel `sent`, shown as `—` when sent is 0; "Active modules" and
  "Auto vs manual" from `GET /platforms/` as today.
- Two new cards under the tiles: **Next actions** (from `next_actions`) and **Storage**
  (§2.4).
- The Generate / Send buttons in the job table and drawer stay visible but, in real mode,
  show `toast.coming_next` and do nothing. In demo mode they keep the fixture behaviour.
  Removing them would regress the visible product.
- Skill match stays the client-side fixture computation with the zero-tags guard fixed
  (`0%`, not `NaN%`). Server-side scoring is sub-project 2.

---

## 4. Documents

### 4.1 Storage, encryption and limits

- **Location.** Files live under `MEDIA_ROOT/documents/org-<id>/` on the
  `findajob_media_data` volume, modes 0600/0700 (already configured). nginx never serves
  `/media/` (already enforced and commented in `deploy/nginx.conf`); every download goes
  through Django (§4.2, §7.1).
- **Filenames.** `document_upload_to` is changed to ignore the incoming filename and return
  `documents/org-<id>/<uuid4().hex>.enc`. The user's original basename goes to
  `Document.name` only. This closes path tricks and stops two members overwriting each
  other's `CV.pdf`. (Django's storage does not do this by itself: it keeps the client name
  and only suffixes on collision.)
- **Encryption at rest.** `web/legal.js` privacy §7 promises, in all three languages with DE
  binding, that documents are "encrypted at rest (AES-256)", and the architecture skill
  lists the same requirement. This sub-project makes it true rather than weakening the
  policy: `apps/jobs/documents/crypto.py` encrypts the sniffed plaintext with AES-256-GCM
  (`cryptography.hazmat`, already a dependency; Fernet is AES-128 and does not satisfy the
  statement). File format: 1 byte key version (`0x01`), 12-byte random nonce, ciphertext,
  16-byte tag; the AAD is `org-<id>`. Key: `DOCUMENT_ENCRYPTION_KEY` (32 bytes, base64), a
  sibling of `OUTBOX_FERNET_KEY`; a new boot check `findajob.E007` in `config/checks.py` fails production start
  when it is missing or the wrong length (same pattern as the existing checks). Files are at
  most 10 MiB, so encrypt/decrypt happens in memory; `size_bytes` is the plaintext size.
  Rotation is a non-goal; the version byte is what makes it possible later. The operator
  may instead choose the honest floor (unencrypted files + rewriting legal §7 in three
  languages and re-running `seed_legal`); that alternative is recorded in §10 and is not the
  default.
- **Accepted types,** checked by extension **and** sniffed content, never by the client's
  `Content-Type`: `pdf`, `doc`, `docx`, `odt`, `png`, `jpg`/`jpeg`. `apps/jobs/documents/sniff.py::detect(fileobj, ext) -> content_type | None`
  is a magic-bytes check (PDF `%PDF-`; PNG signature; JPEG `FF D8 FF`; OLE2
  `D0 CF 11 E0 A1 B1 1A E1` for `.doc`; ZIP containing `[Content_Types].xml` for `.docx`; ZIP
  whose first entry is `mimetype` for `.odt`). The sniffed MIME is stored in
  `Document.content_type`. No new dependency.
- **Limits.** Per file `DOCUMENT_MAX_FILE_BYTES` = 10 MiB; per org
  `DOCUMENT_STORAGE_LIMIT_BYTES` = 50 MiB. Both settings in `config/settings/base.py`. The
  per-file cap is checked first, outside any transaction, from `UploadedFile.size`. The
  per-org cap and the row insert run inside one `transaction.atomic()` block that first takes
  `Organization.objects.select_for_update(of=("self",)).get(pk=org.pk)` (the idiom
  `apps/accounts/services.py` already uses), then computes `storage_for(org)`, rejects with
  `storage_quota_exceeded` if `used + file.size > limit`, and only then creates the row while
  the lock is held. This serialises uploads per org on Postgres (READ COMMITTED would
  otherwise let two concurrent uploads both pass) and is also correct on SQLite. If anything
  raises after the file has been written and before commit, the view deletes the stored file
  in the `except` branch. nginx already allows 12m on `/api/`;
  `DATA_UPLOAD_MAX_MEMORY_SIZE` (5 MiB) governs non-file form data only.
- **File lifecycle.** Django never unlinks a `FileField`'s bytes when its row goes, and
  `Document.org` / `Document.user` are CASCADE, so an account erasure (`user.delete()`, the
  Art. 17 flow), a workspace deletion or an admin delete would otherwise leave CVs on the
  volume forever. Removal is therefore a `post_delete` receiver on `Document` in
  `apps/jobs/signals.py`, connected in `JobsConfig.ready()` (`apps/jobs/apps.py` gains
  `ready`, same pattern as `apps/accounts/apps.py`), so it fires for the API view, the admin
  and every CASCADE. The receiver captures `instance.file.name` and the storage, then
  schedules `transaction.on_commit(remove, robust=True)`, where `remove` calls
  `storage.delete(name)` in try/except and logs `findajob.jobs` WARNING
  `document.file_unlink_failed` with org id and stored name (never `Document.name`) on any
  exception. `robust=True` is required: the row is already committed, so a storage error
  must never surface as a 500. Because a listener is registered, Django's collector loads
  the instances instead of fast-deleting them, and `base_manager_name = "all_objects"` means
  collection never raises `OrgScopeError`.

### 4.2 Endpoints (session + `IsOrgMember`; error envelope from `apps/common/errors.py`)

`POST /documents/` sets `parser_classes = [MultiPartParser, FormParser]` on that view only;
the project default stays JSON-only (`REST_FRAMEWORK["DEFAULT_PARSER_CLASSES"]`). A JSON
body on this route is therefore DRF's 415 `unsupported_media_type`, while a multipart body
carrying a disallowed file is our 415 `unsupported_file_type`; `api.js` maps both.

| Method & path | Body / params | Response | Errors (`code`) |
|---|---|---|---|
| `GET /documents/` | `?kind=` (a `DocumentKind` value) | org-wide list of Document | `invalid_kind` 400 |
| `POST /documents/` | multipart: `file` (req), `name` (opt, defaults to the upload's basename), `kind` (opt, default `other`), `tags` (opt) | `201` Document | `unsupported_media_type` 415 (DRF) · `unsupported_file_type` 415 · `file_too_large` 413 · `storage_quota_exceeded` 413 · `invalid` 400 with `fields` |
| `PATCH /documents/:id/` | JSON: `name`, `kind`, `tags` | Document | `not_found` 404 · `permission_denied` 403 · `invalid` 400 |
| `DELETE /documents/:id/` | | `204`. Inside `transaction.atomic()`: in-use check → `document_in_use` 409; audit row (before `delete()`, §7); row delete. The view does not touch the file; removal is the model's job (§4.1 file lifecycle). 204 whether or not the storage delete later succeeds. | `not_found` 404 · `permission_denied` 403 · `document_in_use` 409 |
| `GET /documents/:id/download/` | | `FileResponse(BytesIO(plaintext), as_attachment=True, filename=doc.name, content_type=doc.content_type)` plus `X-Content-Type-Options: nosniff` and `Cache-Control: private, no-store`. Django builds the RFC 6266/8187 `Content-Disposition` itself (ASCII names quoted, others `filename*=utf-8''` percent-encoded); the header is never formatted by hand. | `not_found` 404 |

- **In-use** is `ApplicationDocument.objects.filter(document=doc).exists()` — never
  `doc.applications`, whose reverse manager is org-guarded and raises `OrgScopeError`.
- **`tags`** (multipart string or JSON list): if the string starts with `[` it must parse as
  a JSON array of strings, else it is split on `,`; each tag trimmed, empty tags dropped,
  duplicates removed preserving order, max 20 tags × 40 chars; anything else → 400 `invalid`
  with `fields.tags`.
- **`name`** (POST and PATCH): DRF `CharField(max_length=255)`, trimmed, non-blank, and
  `validate_name` rejects any character below 0x20 or 0x7F and any `/` or `\` → `invalid`
  with `fields.name`. The same rule applies to `Contact.name`.
- **Validation errors** come from `serializer.is_valid(raise_exception=True)`; the envelope
  is `{code: "invalid", detail, fields: {name: […]}}` (the code the shared handler already
  produces; there is no `validation_error`).

Document wire shape (also the updated fixture shape):

```json
{"id": 3, "name": "CV — Backend (EN).pdf", "kind": "cv", "tags": ["Django","Python"],
 "size_bytes": 188416, "content_type": "application/pdf", "ats_score": null,
 "created_at": "2026-09-20T09:00:00Z", "updated_at": "2026-09-20T09:00:00Z",
 "download_url": "/api/v1/documents/3/download/"}
```

`ats_score` is `null` until scoring exists (a non-goal). When it is `null` the card omits
the whole ATS row — label, number and bar — exactly as the current `hasAts` guard does, for
every `kind`; no `—` placeholder. The fixtures keep their three CV scores and carry
`ats_score: null` on the certificate, diploma and other entries, so the demo grid is
unchanged.

**Ownership.** A document belongs to its uploader (`user`) inside the org. Every member of
the org can **list and download** org documents (the Team plan is a shared workspace); only
the uploader or an org admin/owner can **modify or delete** — a same-org member who is
neither gets 403 `permission_denied` (the row exists and they may read it, so 404 would lie).
Cross-org and unknown ids are 404 `not_found` because the org-scoped lookup never finds
them. Enforcement is in §7.

### 4.3 Screen

The current Documents screen (cards grid, kind pills, tags, ATS badge) plus:

- Upload button → native file picker; also drag-and-drop onto the grid (§2.4 drop zone).
  Progress via `XMLHttpRequest` upload events (the one place `fetch` cannot report progress;
  still owned by `api.js` as `uploadDocument(file, meta, onProgress)`, which sends the
  `X-CSRFToken` header from `readCookie('csrftoken')` exactly as `request()` does — DRF's
  `SessionAuthentication` enforces CSRF on it).
- Card menu: Rename, Edit tags, Change kind, Download, Delete (confirm in a drawer with a
  `.btn-danger`, never a browser `confirm()`).
- Empty state and storage card (§2.4). The storage card reads `GET /dashboard/`.

---

## 5. Networking CRM

### 5.1 Model — `apps/jobs/models.py::Contact`

Org-scoped like `Document`; `OrgScopedManager(user_field="user")`, `all_objects` fallback,
`default_manager_name`/`base_manager_name` set the same way.

| Field | Type | Notes |
|---|---|---|
| `org`, `user` | FK | CASCADE; `user` = who created it |
| `name` | char 255 | required; same validation as `Document.name` |
| `role` | char 255 | blank |
| `company` | char 255 | blank |
| `email` | email | blank |
| `linkedin_url` | url | blank |
| `source` | char 64 | free text: `LinkedIn`, `Wellfound`, `Referral`, a platform slug… |
| `status` | choices | `to_contact` (default) · `messaged` · `replied` · `referred` |
| `last_touch` | date | null; set on `advance`, editable via PATCH |
| `next_action` | char 255 | blank |
| `next_action_at` | date | null |
| `notes` | text | blank |
| `job_listing` | FK | null, SET_NULL — optional link to a listing |
| `created_at`, `updated_at` | | |

Indexes: `(org, status)`, `(org, next_action_at)`. No uniqueness constraint (the same person
can legitimately appear twice across roles); the UI warns on a duplicate email.

Contacts are third-party personal data the member enters about other people. `web/legal.js`
privacy already covers "data you enter" generically; the DPA/privacy text is **not** changed
in this sub-project, and `notes`/`email` are never written to logs or audit payloads (§7).

### 5.2 Endpoints (session + `IsOrgMember`)

| Method & path | Body / params | Response | Errors |
|---|---|---|---|
| `GET /contacts/` | `?status=&q=` (q matches name/company/role, `icontains`) | org-wide list, ordered `next_action_at` ascending nulls last, then `-updated_at` | `invalid_status` 400 |
| `POST /contacts/` | JSON of any writable field; `name` required | `201` Contact | `invalid` 400 with `fields` |
| `PATCH /contacts/:id/` | JSON partial | Contact | `not_found` 404 · `permission_denied` 403 · `invalid` 400 |
| `DELETE /contacts/:id/` | | `204` | `not_found` 404 · `permission_denied` 403 |
| `POST /contacts/:id/advance/` | none | Contact with the next status, `last_touch = timezone.localdate()` (UTC; `TIME_ZONE` is pinned to UTC by a contract test — the client may PATCH `last_touch` afterwards if the day is wrong) | `not_found` 404 · `permission_denied` 403 · `already_final` 409 when status is `referred` |

Visibility mirrors Documents: all org members read; creator or admin/owner writes (§7).

Contact wire shape (and updated fixture shape):

```json
{"id": 2, "name": "Tom Okafor", "role": "Eng Manager", "company": "Vercel",
 "email": "", "linkedin_url": "", "source": "LinkedIn", "status": "replied",
 "last_touch": "2026-07-30", "next_action": "Send CV — he offered referral",
 "next_action_at": "2026-08-04", "notes": "", "job_listing": null,
 "created_at": "…", "updated_at": "…"}
```

### 5.3 Screen

The current Network screen (table: name, role, company, source, status pill, last touch,
next action) plus:

- Status filter pills (All / To contact / Messaged / Replied / Referred) and a search box.
- "Add contact" → right drawer (560px, same chrome as the Review drawer) with the form.
- The contact **name** in the first cell is a `<button class="link">` (Inter 600, ink, hover
  underline in `#10b981`) that opens the same drawer in edit mode; the row itself is not
  clickable. The status pill in the table is a static `<span>`; **Advance** and **Delete**
  live only in the drawer footer (in demo mode Advance does the same in-memory walk). The
  current "Click a status pill to advance it" hint line is removed.
- Empty state (§2.4).

---

## 6. `web/api.js` additions

```js
ENDPOINTS.dashboard = '/dashboard/';
ENDPOINTS.documents = '/documents/';            // + /:id/, /:id/download/
ENDPOINTS.contacts  = '/contacts/';             // + /:id/, /:id/advance/

// new tiny helper (URLSearchParams, drops null/empty values); getJobs is refactored to use it
export const withQuery = (path, params = {}) => …;

export const getDashboard   = () => request(ENDPOINTS.dashboard);
export const getDocuments   = (params) => request(withQuery(ENDPOINTS.documents, params));
export const uploadDocument = (file, meta, onProgress) => …   // XHR, X-CSRFToken, ApiError on failure
export const updateDocument = (id, patch) => request(`${ENDPOINTS.documents}${id}/`, {method:'PATCH', body: patch});
export const deleteDocument = (id) => request(`${ENDPOINTS.documents}${id}/`, {method:'DELETE'});
export const getContacts    = (params) => request(withQuery(ENDPOINTS.contacts, params));
export const createContact  = (body) => request(ENDPOINTS.contacts, {method:'POST', body});
export const updateContact  = (id, patch) => …;
export const deleteContact  = (id) => …;
export const advanceContact = (id) => request(`${ENDPOINTS.contacts}${id}/advance/`, {method:'POST'});

export const liveApi = { getMe, getPlatforms, getJobs, getDashboard, getDocuments, uploadDocument, … };
export function createDemoApi() { /* same names over an in-memory copy of the fixtures */ }
```

`request()` is unchanged. `uploadDocument` raises the same `ApiError` from the same
envelope, and also reports `network_unreachable` on XHR `error`/`timeout`.

---

## 7. Backend layout

```
apps/jobs/
  apps.py                  + ready() importing signals
  models.py                + Contact, ContactStatus; Document.content_type (char 64);
                             document_upload_to → uuid .enc
  serializers.py           + DocumentSerializer, ContactSerializer (ModelSerializers)
  views.py                 (unchanged)
  views_documents.py       DocumentListCreateView, DocumentDetailView, DocumentDownloadView
  views_contacts.py        ContactListCreateView, ContactDetailView, ContactAdvanceView
  views_dashboard.py       DashboardView
  services_dashboard.py    funnel_for(), counts_for(), storage_for()   # flat module:
                           # apps/jobs/services.py (ingest_platform) already exists and a
                           # services/ package would shadow it
  documents/sniff.py       detect(fileobj, ext) -> content_type | None
  documents/crypto.py      encrypt(plaintext, aad) / decrypt(blob, aad), key loading
  signals.py               on_document_deleted (post_delete → on_commit unlink)
  urls.py                  + dashboard/, documents/…, contacts/…
  migrations/000X_contact.py
  migrations/000Y_document_content_type.py
  admin.py                 + ContactAdmin (org column, status filter); DocumentAdmin (§7.1)
apps/tenancy/permissions.py + IsRowOwnerOrOrgAdmin
config/checks.py           + DOCUMENT_ENCRYPTION_KEY boot check (findajob.E007)
config/settings/base.py    + DOCUMENT_STORAGE_LIMIT_BYTES, DOCUMENT_MAX_FILE_BYTES,
                             DOCUMENT_ENCRYPTION_KEY
config/settings/test.py    + MEDIA_ROOT = tempfile.mkdtemp(...), a test encryption key
```

Views subclass DRF `APIView` like the existing ones. Querysets always start from
`Model.objects.for_org(membership.org)`; that is the first line of defence and the reason a
cross-org or unknown id is 404, never 403.

**Row ownership.** `apps/tenancy/permissions.py` gains `IsRowOwnerOrOrgAdmin(IsOrgMember)`:
`has_permission` is inherited (any member); `has_object_permission` first runs the inherited
org check, then returns `True` for `request.method in SAFE_METHODS`, otherwise
`obj.user_id == request.user.id or current_membership(request).is_admin`.
`message = "Only the creator or a workspace admin can change this."` — DRF maps the refusal
to 403, which the shared handler renders as `permission_denied`. `DocumentDetailView`,
`ContactDetailView` and `ContactAdvanceView` set `permission_classes = [IsRowOwnerOrOrgAdmin]`;
list/create, dashboard and `DocumentDownloadView` keep `[IsOrgMember]`. Because the views
are plain `APIView`, `has_object_permission` never runs on its own: every detail view fetches
through one helper, `get_org_object(request, Model, pk)`, which does the scoped lookup,
raises `ApiError(ErrorCode.NOT_FOUND, …, 404)` on miss and then calls
`self.check_object_permissions(request, obj)`.

**Audit.** Create and delete of documents and contacts append one `AuditLog` row via the
single blessed writer `AuditLog.objects.record(...)`, inside the same `transaction.atomic()`
as the write and, for deletes, **before** `obj.delete()` (Django nulls `obj.pk` after delete).
Actions follow the existing `<noun>.<verb>` form: `document.upload`, `document.delete`,
`contact.create`, `contact.delete`. Call shape:

```python
AuditLog.objects.record(
    action="document.upload", org=membership.org, actor=request.user, target=obj,
    payload={"kind": obj.kind, "size_bytes": obj.size_bytes},   # contacts: {"status": obj.status}
    ip=client_ip(request), user_agent=client_user_agent(request),
)
```

with `from apps.legal.request_meta import client_ip, client_user_agent` — never
`request.META["REMOTE_ADDR"]` (nginx's container address in prod) and never
`X-Forwarded-For[0]`. Payloads never carry the filename, `name`, `email`, `notes` or `tags`.

### 7.1 DocumentAdmin download

`Document.file` is a `FileField`, so the default change form renders a `Currently:
/media/…` link, which is dead by design in production (nginx never serves `/media/`;
`config/urls.py` mounts it only under `DEBUG`), and the API download route is
`IsOrgMember`-gated, so it 404s for an operator with no membership in the member's org.
Therefore: `file` goes to `exclude`, `stored_path` (`obj.file.name`) is a read-only field, and
a `download` column in `list_display` links to a staff-only route registered in
`DocumentAdmin.get_urls()` as `<int:pk>/download/` (name `jobs_document_download`), wrapped
in `self.admin_site.admin_view(...)` so it inherits the staff check and admin-login redirect.
That view loads the row via `Document.all_objects` (never `.objects`), decrypts through the
same `documents/crypto.py`, returns the same `FileResponse` as §4.2, and records
`AuditLog` action `admin.document_download` with the target and the operator as actor.

---

## 8. Testing

`backend/tests/jobs/`:

- `test_dashboard.py` — empty account → all zeros, all nine `applications` keys present,
  empty lists; funnel cumulative rule incl. `sending` in review and `failed` excluded;
  `jobs_new` counts `created_at` not `scraped_at`; `jobs_pending` excludes listings with an
  application row; documents/contacts/storage are org-wide while applications are
  member-own; org isolation; overdue `next_actions` ordering; 401 anonymous.
- `test_documents.py` — POST multipart is accepted (not 415); upload each accepted type;
  reject wrong extension; reject right extension with wrong bytes (`unsupported_file_type`);
  10 MiB + 1 byte → `file_too_large`; quota → `storage_quota_exceeded`; two uploads that
  together exceed the quota cannot both succeed; stored path is `documents/org-<id>/<hex>.enc`
  and the bytes on disk are not the plaintext; `content_type` stored from the sniff; download
  returns the plaintext with `Content-Disposition` starting `attachment;` whose decoded
  filename equals `doc.name` (incl. the non-ASCII example name), `nosniff`, `no-store`;
  `name` with a control character or slash → `invalid` with `fields.name`; `tags` parsing
  rules; `?kind=` invalid → 400; cross-org id → 404; same-org non-owner PATCH/DELETE → 403
  `permission_denied`; admin member may delete; delete removes the file from disk; a rolled
  back delete leaves the file; `user.delete()` and `org.delete()` remove files; delete of an
  attached document → 409; `ats_score` is null; audit rows written with the right action and
  no filename in payload.
- `test_contacts.py` — CRUD, `q`/`status` filters, ordering, advance walks all four states
  then 409, `last_touch` set, cross-org 404, same-org non-owner 403, read-by-any-member.
- `test_admin_document_download.py` — staff route decrypts and streams; anonymous is
  redirected to admin login; audit row written.
- `tests/test_endpoints_contract.py` (new) — parse `ENDPOINTS` from `web/api.js` with
  `apps.legal.jsparse` (no Node), assert every path this spec adds (`/dashboard/`,
  `/documents/`, `/contacts/`) plus `/me/`, `/platforms/`, `/jobs/` resolves via
  `django.urls.resolve("/api/v1" + path)`; entries in `ASPIRATIONAL = {generate, send,
  profile, integrations, org, billing, skillmatch}` are skipped until their sub-project
  lands. Plus the `errors.*` i18n coverage check from §2.1.
- `test_project_contract.py` gains: `apps.jobs` imports nothing from `apps.accounts` beyond
  the user model; the boot check refuses a missing `DOCUMENT_ENCRYPTION_KEY` outside DEBUG.

**Test isolation for files.** The suite is plain `django.test.TestCase`, which never
commits, so `transaction.on_commit` callbacks do not run on their own, and no test may touch
the real `backend/media`:

- `config/settings/test.py` gains `MEDIA_ROOT = Path(tempfile.mkdtemp(prefix="findajob-test-media-"))`
  (one directory per test process) and a fixed test `DOCUMENT_ENCRYPTION_KEY`. It lives in
  the settings module, not a `conftest.py`, because the suite must also pass under
  `DJANGO_SETTINGS_MODULE=config.settings.test manage.py test`. Today
  `tests/jobs/test_models.py` and `tests/admin/support.py` already leak stub documents into
  `backend/media/documents/org-1/`; those leftovers are deleted as part of this change.
- Every test that asserts on-disk state after a write goes through the view inside
  `with self.captureOnCommitCallbacks(execute=True):`. Do not switch to `TransactionTestCase`.
- Upload samples are built in memory by `tests/jobs/uploads.py::sample(ext) ->
  SimpleUploadedFile`, not committed as binaries: pdf `b'%PDF-1.4\n…%%EOF\n'`; png = the
  8-byte signature + a minimal IHDR chunk; jpg `b'\xff\xd8\xff\xe0' + 16 zero bytes +
  b'\xff\xd9'`; doc = the OLE2 signature + 504 zero bytes; docx = `zipfile.ZipFile` with a
  `[Content_Types].xml` entry; odt = `zipfile.ZipFile` whose first entry is `mimetype` stored
  uncompressed. "Right extension, wrong bytes" pairs any body with any name.

Frontend has no test runner (project rule). Verification is manual in the browser on
`python3 -m http.server` + `runserver`, both modes (`?demo=1` and signed-in), at 1280 px
(desktop), 800 px (inside the ≤899 band: sidebar starts collapsed, grids 2-up, header
wraps, table cards scroll), 560 px (all grids 1-up) and 390 px (phone; no horizontal page
scroll), in all three languages, with the checklist recorded in the implementation plan.
A width equal to or above a breakpoint's `max-width` does not exercise it.

---

## 9. Deploy

Order, per the `findajob-deploy` skill:

1. **nginx A** (no-cache rule with the new names, `support` and `'unsafe-eval'` kept) —
   harmless before the files exist.
2. **Backend image** with the two additive migrations, `DOCUMENT_ENCRYPTION_KEY` set in the
   VPS env first (the boot check refuses to start without it); `docs/2026-08-01-database-schema.md`
   is updated with the two new tables/columns in the same commit.
3. **`rsync web/`** (new shell). Until step 2 is live, a new shell on an old backend shows
   the error card on Dashboard/Documents/Network (§2.3), never fixtures — acceptable for the
   minutes between steps, and the reason step 2 precedes step 3.
4. **nginx B**: drop `support` from the alternation and `'unsafe-eval'` from `script-src`;
   `docker compose up -d --force-recreate web` (a reload is not enough for the CSP header).
5. `verify_platform`.

**`verify_platform` changes**, keeping the module split of the verify design spec:

- `api_gate` gains `GET /api/v1/dashboard/ unauthenticated → 401 canonical`, built exactly
  like the `/me/` check; **404 → SKIP "not deployed"**, never FAIL (the degradation rule).
- `frontend`: `SUPPORT_FILES` becomes `["/api.js", "/legal.js", "/app.js", "/app.css",
  "/i18n.js", "/ui.js", "/consent-banner.js", "/analytics.js"]`; a new check FAILs if the
  served `/app.html` contains `support.js` or `window.__resources`; the `_RESOURCES` regex,
  `resource_urls()` and their probe loop are deleted (they could only ever return `[]` once
  the map is gone — a silent no-op, not a check). The pure helper
  `references_legacy_runtime(html) -> bool` gets a unit test.

**Rollback** of the frontend is two steps in this order: (1) restore the pre-B `nginx.conf`
and `--force-recreate web`, because the old shell needs `'unsafe-eval'`; (2) restore `web/`
from the `.deploy-state/web-<TS>` snapshot `deploy.sh` prints (kept for the last five web
deploys) or by reverting the commit. The migrations are additive and need no reverse; an
api rollback after the web deploy leaves the three real-data screens on the error card
until the backend is redeployed.

---

## 10. Risks and decisions

- **Rewrite size.** Thirteen screens ported by hand from a 139 KB export. Mitigation: port
  the shell + Dashboard + Documents + Network first on real data, then the ten fixture
  screens one by one, each compared side by side with the old `app.html` served from a
  backup path. The old file is kept in git history, not in `web/`.
- **Encryption at rest is the default (§4.1).** The alternative — unencrypted files, rewrite
  `web/legal.js` §7 in EN/DE/RO to the honest floor ("access-controlled volume, 0600, served
  only through an authenticated view, TLS") and re-run `seed_legal` — is cheaper but weakens
  a published legal statement and the architecture requirement. The operator decides; the
  spec is written for encryption.
- **Anonymous `/app.html` shows the demo, not a login.** This is today's behaviour and the
  code's explicit rationale; CLAUDE.md's "redirect on 401" sentence is corrected. If the
  operator wants a redirect instead, it must still exempt the legal routes (§ 5 DDG).
- **Shared org documents and contacts.** A Team workspace shares both by design; a Pro/Free
  org has one member so the distinction is invisible. A future `visibility` field is additive.
- **No virus scanning.** Documents are only ever downloaded by the org that uploaded them
  (and by staff via §7.1) and sent as attachments by the uploader; sniffing + `nosniff` +
  `attachment` disposition is the accepted floor. Recorded so it is a decision, not an
  omission.
- **Key management.** One symmetric key in the VPS env, like `OUTBOX_FERNET_KEY`. Losing it
  loses every document; the deploy skill's backup routine must include the env file (already
  the case for `OUTBOX_FERNET_KEY`; verify during the plan).
