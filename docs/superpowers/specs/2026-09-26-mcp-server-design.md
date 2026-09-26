# MCP server — findajob.pro as a tool for Claude / ChatGPT (spec, 2026-09-26)

Status: **proposed**. Nothing implemented. Phase 1 needs one product decision
(§9) before code is written.

## 1. Why

Claude Desktop, claude.ai and ChatGPT now let a person plug third-party
services into the chat through the Model Context Protocol (MCP). Job boards
publish official connectors (Indeed among them). That creates two
opportunities for findajob.pro, and this spec covers only the first:

1. **findajob.pro as an MCP server.** A member connects their workspace to
   the assistant they already use and asks it "which new jobs match me, draft
   the founder message for the Wellfound one, mark the LinkedIn one as sent".
   The assistant does the reasoning; findajob.pro stays the system of record
   for the pipeline, the documents and the send-from-own-mailbox flow.
2. findajob.pro as an MCP **client** that ingests from official connectors
   (an `mcp` adapter under `apps/jobs/adapters/`). Out of scope here. It needs
   per-user OAuth token storage, which phase 2 of this spec builds anyway, so
   it gets its own spec afterwards.

Value of (1): the pipeline, funnel analytics, TTDSG consent and the
send-from-user-mailbox rule are what a bare "chat + Indeed connector" cannot
do. The MCP server exposes them without a new UI.

## 2. Principles (inherited, non-negotiable)

- **The gate is the API.** Every tool call resolves to the same org-scoped
  querysets the browser uses. No tool bypasses `OrgScopedManager`.
- **Per-platform risk rules are law.** No tool ever submits on LinkedIn,
  Indeed or Wellfound. `send_application` refuses unless the platform has
  `auto_send=True` and `apply_mechanism="email"`, exactly like the outbox
  spec. High-risk platforms get `mark_sent_manually` and nothing else.
- **Manual review is mandatory.** A tool can create or edit a draft and move
  it to `REVIEW`. Moving to `SENT` is a separate, explicit tool call.
- **The server generates nothing.** The assistant on the other end writes
  the message; findajob.pro stores it. No Anthropic API call happens inside
  a tool. That keeps the server cheap, deterministic and testable.
- **One error shape.** Inside JSON-RPC the payload keeps
  `{"error": {"code", "detail"}}` as the tool result's `structuredContent`,
  so the assistant sees the same machine codes as `web/api.js`.
- **No heavy dependency without asking.** See §9.

## 3. Transport and mount point

- Protocol: MCP **Streamable HTTP**, single endpoint, JSON-RPC 2.0 over POST.
  Phase 1 answers every request with a plain JSON response and does **not**
  open SSE streams (the spec allows this; clients fall back cleanly). No
  server-initiated messages, no resumability, so no state on the server
  beyond the auth token.
- Path: **`/mcp/`**, mounted in `config/urls.py` next to `webhooks/email/`,
  i.e. *outside* `/api/v1/`. Reason: it is machine-to-machine, bearer-only,
  CSRF-exempt and JSON-RPC, so it must not inherit the DRF defaults
  (session auth, `IsAuthenticated`, the DRF exception handler) that make
  `/api/v1/` safe for the browser. Deploy: nginx gains one `location /mcp/`
  proxying to the api container, same as `/api/`. Recorded in the
  `findajob-deploy` skill when it lands.
- Methods handled: `initialize`, `notifications/initialized`, `ping`,
  `tools/list`, `tools/call`.
  Everything else → JSON-RPC `-32601 method not found`.
- `GET /mcp/` → 405. `DELETE /mcp/` → 204 (session teardown is a no-op).
- Protocol version advertised: `2025-06-18`. Server info
  `{"name": "findajob.pro", "version": "<git sha>"}`.

## 4. Authentication

### Phase 1 — personal access tokens (PAT)

Enough for Claude Code, Claude Desktop (via `mcp-remote --header`) and any
self-hosted agent. Not enough for claude.ai web or ChatGPT, which require
OAuth (phase 2).

New model in **`apps.tenancy`** (it is an org+member credential, like
`MailboxConnection`; `apps.accounts` stays pinned to its three models):

**`McpToken`** — org-scoped via `OrgScopedManager(user_field="user")`.

| field | notes |
|---|---|
| `org`, `user` | the workspace and member the token acts as |
| `name` | label shown in Settings ("Claude Desktop – laptop") |
| `prefix` | first 8 chars of the token, shown in the UI for identification |
| `token_hash` | SHA-256 of the full token; the plaintext is shown **once** at creation and never stored |
| `scopes` | JSON list, subset of §6 |
| `expires_at` | default 90 days; null allowed only for `owner`/`admin` roles |
| `last_used_at`, `last_used_ip` | updated at most once per minute per token |
| `revoked_at` | soft revoke; revoked tokens fail with `token_revoked` |

Token format: `fjp_` + 40 url-safe random chars. Lookup is by `prefix` then
constant-time compare of the hash.

`Authorization: Bearer fjp_…` on every request. Failures return HTTP 401
with `WWW-Authenticate: Bearer` and the canonical error envelope
(`token_invalid`, `token_expired`, `token_revoked`). No token → 401, never
a session fallback: a browser session must not be able to drive `/mcp/`
because that would re-open CSRF.

Every accepted request binds `request.user` and `request.org` from the
token, and the membership is re-checked on each call (a removed member's
tokens die with the membership: `Membership` delete cascades `McpToken`).

Management endpoints, session-authenticated, under `/api/v1/mcp-tokens/`:

| method | route | behaviour |
|---|---|---|
| GET | `mcp-tokens/` | list own tokens in current org (prefix, name, scopes, dates; never the hash) |
| POST | `mcp-tokens/` | create; body `{name, scopes, expires_in_days}`; response includes `token` **once** |
| DELETE | `mcp-tokens/<id>/` | revoke; 204 |

Each create/revoke writes an `AuditLog` row (`mcp_token.created`,
`mcp_token.revoked`).

### Phase 2 — OAuth 2.1 authorization server

Required for claude.ai web connectors and ChatGPT. findajob.pro becomes an
OAuth AS with `/.well-known/oauth-authorization-server`, PKCE, dynamic client
registration and a consent screen that reuses the session login. The issued
access token maps to the same `McpToken` row (`kind = "oauth"`), so the
authorisation layer in §4/§6 does not change. Specified later; not blocking.

## 5. Tools

All tools run inside `transaction.atomic()`, take the org/user from the
token, and return `structuredContent` plus a short `text` rendering for
clients that ignore structured output. Input schemas are JSON Schema and are
generated from DRF serializers so the API and the tools cannot drift.

| tool | scope | does |
|---|---|---|
| `list_jobs` | `jobs:read` | Filter by `platform`, `q`, `since`, `limit ≤ 50`. Returns the `JobListing` serializer shape plus `platform.risk`, `platform.apply_mechanism`, `platform.auto_send`, and the member's `application_status` if one exists. |
| `get_job` | `jobs:read` | One listing, full `description` and `requirements`. |
| `list_applications` | `applications:read` | Own applications in current org, filter by `status`. |
| `get_application` | `applications:read` | Full draft including `generated_content`, `generated_cover_letter`, attached document names. |
| `draft_application` | `applications:write` | `{job_id, subject, content, cover_letter?, notes?, document_ids?}` → creates `Application` in `REVIEW`. Refuses with `application_exists` if one already exists for this job and member. Validates `document_ids` through the org-scoped `Document` manager. |
| `update_application` | `applications:write` | Edit the same fields on an application in `PENDING`/`REVIEW`. Refuses on `SENT` and later (`application_locked`). |
| `mark_sent_manually` | `applications:send` | Allowed **only** when `platform.auto_send is False`. Sets `sent_manually=True`, `sent_at=now`, status `SENT`. This is the "Copy & mark as sent" gesture for LinkedIn / Indeed / Wellfound. |
| `send_application` | `applications:send` | Allowed **only** when `platform.auto_send and apply_mechanism == "email"` and the member has a `MailboxConnection` in `connected` state. Delegates to the outbox send path (outbox spec phase 3). Until that lands the tool is registered but returns `not_implemented` so clients can discover it. |
| `get_profile` | `profile:read` | **Deferred.** `UserProfile` / `SearchProfile` do not exist in the backend yet (audited 2026-09-26: no model, no migration). The tool is added in the same change that creates those models. The scope is reserved now so tokens created today do not need re-issuing. |

Deliberately absent: anything that creates a job, edits platforms, touches
billing, members, legal, or sends OTP mail. No tool takes a URL and fetches
it (no SSRF surface).

Every tool that mutates writes an `AuditLog` row with
`action = "mcp.<tool>"`, `actor = token.user`, `payload = {token_prefix, …}`,
so an operator can tell a chat-driven change from a UI one.

### Resources

Deferred with `get_profile`. Phase 1 advertises no `resources` capability;
`resources/list` and `resources/read` answer `-32601`. When they land they
mirror `get_profile` / `get_job` and add no new authorisation path.

## 6. Scopes

`jobs:read`, `applications:read`, `applications:write`, `applications:send`,
`profile:read`. A tool call without the required scope returns the tool
result `{"error": {"code": "scope_missing", "detail": "…"}}` with
`isError: true`. Default scope set on token creation in the UI:
everything except `applications:send`, which the member must tick.

## 7. Limits and failure modes

- Per-token rate limit: 60 `tools/call` per minute, 600 per hour, cache
  backed. On cache failure this **fails closed** (same policy as the OTP
  verify budget): the assistant gets `rate_limited`, the browser is
  unaffected.
- Daily send caps are enforced inside the outbox path, not duplicated here.
- Request body cap 256 KB; JSON-RPC batches are rejected (`-32600`) to keep
  the atomic-per-call guarantee simple.
- Any unhandled exception inside a tool → JSON-RPC result with
  `isError: true` and code `server_error`; the traceback goes to the
  standard logger, which is behind `RedactingFilter`. Tokens never appear
  in logs (the filter gains an `fjp_[A-Za-z0-9_-]{40}` pattern).

## 8. Files

```
backend/apps/tenancy/mcp/
  __init__.py
  models.py        McpToken  (+ migration in apps/tenancy/migrations/)
  auth.py          bearer parsing, hash compare, membership re-check
  rpc.py           JSON-RPC envelope, method dispatch, error mapping
  tools.py         the tool registry: name → (scope, input schema, handler)
  tools_jobs.py    list_jobs / get_job
  tools_applications.py  draft / update / mark_sent_manually / send
  ratelimit.py     per-token budget, fails closed
  views.py         McpEndpoint (csrf_exempt, POST/DELETE, no DRF auth classes)
  token_views.py   /api/v1/mcp-tokens/ (DRF, session auth)
  urls.py          "" → McpEndpoint ; token_urls.py for the API side
backend/apps/tenancy/admin.py   McpToken read-only list (prefix, user, org,
                                scopes, last_used_at, revoke action)
config/urls.py   path("mcp/", include("apps.tenancy.mcp.urls"))
                 + "mcp-tokens/" under api_v1_patterns
web/app.html     Settings → "Connected assistants": list, create (shows the
                 token once, with the Claude Desktop / mcp-remote snippet),
                 revoke. Fixture-driven in demo mode like every other screen.
tests/tenancy/mcp/
  test_auth.py       401 paths, expired/revoked, cross-org token cannot see
                     another org's rows, session cookie alone is rejected
  test_rpc.py        initialize/tools_list/unknown method/batch rejected
  test_tools.py      each tool happy path + every refusal in §5, especially:
                     mark_sent_manually on a LOW platform refused,
                     send_application on LinkedIn refused, REVIEW-only edits
  test_ratelimit.py  fails closed on cache error
```

Fits the `OrgScopeError` discipline: every handler starts from
`Model.objects.for_org(request.org)`; tests assert that
`JobListing`'s global reads never leak `Application` rows across orgs.

## 9. The one open decision — implementation of the protocol layer

Option A (recommended): **hand-rolled JSON-RPC in a Django view**, about
250 lines for the seven methods in §3. Zero new dependencies, no ASGI, runs
under the existing gunicorn WSGI worker, testable with the Django test
client. Cost: we own protocol conformance ourselves and must bump
`protocolVersion` by hand.

Option B: the official `mcp` Python SDK (`FastMCP`). Gives conformance,
SSE streaming and the OAuth helpers for phase 2 for free. Cost: it is an
ASGI/Starlette app, so it means mounting a second ASGI app beside Django or
running a second process, plus `anyio`, `httpx`, `starlette`, `pydantic`
as new pins. That is the "heavy new dependency" the hard rules say to ask
about.

Recommendation: A for phase 1. Re-evaluate B when phase 2 (OAuth) starts,
because that is where the SDK's helpers earn their weight.

## 10. Rollout

1. Phase 1 backend + Settings screen behind a feature flag
   `MCP_SERVER_ENABLED` (default False in prod until nginx has the route).
2. `verify_platform` gains a `mcp` check module: `initialize` with an invalid
   token must return 401 with the envelope; with `MCP_VERIFY_TOKEN` set it
   calls `tools/list` and asserts the §5 names. SKIP when the env var is
   absent.
3. Docs: this spec, a "Connect to Claude Desktop" section in the help page,
   and the nginx line in the deploy skill.

## 11. Out of scope

MCP client / Indeed ingestion adapter; OAuth AS (phase 2); server-side AI
generation; prompts/sampling capabilities; SSE streaming; any tool that
automates LinkedIn, Indeed or Wellfound.
