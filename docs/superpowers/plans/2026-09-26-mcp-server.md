# MCP Server (phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose findajob.pro to Claude Desktop / Claude Code as an MCP server: bearer-token auth, a JSON-RPC endpoint at `/mcp/`, and six org-scoped tools that never automate a high-risk platform.

**Architecture:** A hand-rolled JSON-RPC 2.0 view (`apps/tenancy/mcp/`) mounted at `/mcp/` outside `/api/v1/`, authenticated only by a `McpToken` bearer (SHA-256 hashed, org+member scoped, scoped permissions), rate-limited fail-closed, dispatching to a tool registry whose handlers reuse the existing `OrgScopedManager` querysets and write `AuditLog` rows. Tokens are managed by a session-authenticated DRF endpoint under `/api/v1/mcp-tokens/`.

**Tech Stack:** Django 6.0 + DRF 3.17 (already pinned), Python 3.12, pytest-django. **No new dependencies.**

**Spec:** `docs/superpowers/specs/2026-09-26-mcp-server-design.md`

## Global Constraints

- Run every command from `backend/`: `cd backend && ../.venv/bin/pytest …`. Never from the repo root.
- No new packages in `requirements.txt` (spec §9 option A). `import mcp` / `starlette` / `pydantic` anywhere is a plan failure.
- Every read of an org-scoped model goes through `.for_org(org)`; `.unscoped()` appears **only** in `auth.py` (pre-auth token lookup) and tests.
- Error envelope everywhere: HTTP failures are `{"error": {"code", "detail"}}`; tool failures are JSON-RPC results with `isError: true` and `structuredContent = {"error": {...}}`.
- `apps.accounts` is never imported from `apps/tenancy/mcp/`.
- Tokens: `fjp_` + 40 url-safe chars (44 chars total). Plaintext returned exactly once. Never logged.
- Scopes, verbatim: `jobs:read`, `applications:read`, `applications:write`, `applications:send`, `profile:read`.
- Protocol version string: `2025-06-18`. Server name: `findajob.pro`.
- Rate limit: 60 `tools/call` per minute and 600 per hour per token, **fails closed** on cache error.
- Request body cap 256 KB. JSON-RPC batch arrays rejected with `-32600`.
- `MCP_SERVER_ENABLED` setting, default `False`; `config/settings/test.py` sets it `True`.
- No tool ever changes state on a platform with `auto_send=False` except `mark_sent_manually`; `send_application` refuses unless `platform.auto_send and apply_mechanism == "email"`.
- Commit after every task with the attribution line `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

## Review Focus

1. **A token from org A used while its user is also a member of org B** must only ever see org A's rows. Pinned in Task 5 (`test_token_is_pinned_to_its_org_not_the_users_other_orgs`).
2. **A browser session cookie alone on `/mcp/`** must be 401, never a CSRF-less write path. Pinned in Task 3 (`test_session_cookie_alone_is_rejected`).
3. **`Authorization: Bearer fjp_…` with the right prefix but wrong tail** must be 401 in constant time, not a 500 or a match. Pinned in Task 3 (`test_wrong_tail_same_prefix_is_401`).
4. **`draft_application` with a `document_id` belonging to another org** must refuse with `document_not_found`, not attach it. Pinned in Task 6.
5. **`mark_sent_manually` on a LOW-risk auto-send platform** must refuse (`use_send_application`) so the funnel never records a "manual" send on a platform that dispatches mail. Pinned in Task 7.

---

## File structure

```
backend/apps/tenancy/mcp/__init__.py            empty
backend/apps/tenancy/mcp/models.py              McpToken, SCOPES, generate_token, hash_token
backend/apps/tenancy/mcp/auth.py                authenticate(request) -> (McpToken, Membership); McpAuthError
backend/apps/tenancy/mcp/ratelimit.py           check_budget(token_id) -> None | raises McpRateLimited
backend/apps/tenancy/mcp/rpc.py                 parse/dispatch JSON-RPC; result/error helpers
backend/apps/tenancy/mcp/tools.py               Tool dataclass, ToolContext, ToolError, REGISTRY, register()
backend/apps/tenancy/mcp/tools_jobs.py          list_jobs, get_job
backend/apps/tenancy/mcp/tools_applications.py  list/get/draft/update/mark_sent_manually/send
backend/apps/tenancy/mcp/views.py               McpEndpoint (plain Django View, csrf_exempt)
backend/apps/tenancy/mcp/urls.py                "" -> McpEndpoint
backend/apps/tenancy/mcp/token_views.py         DRF: list/create/revoke McpToken
backend/apps/tenancy/mcp/token_serializers.py
backend/apps/tenancy/mcp/token_urls.py
backend/apps/tenancy/migrations/0002_mcptoken.py
backend/apps/tenancy/admin.py                   + McpTokenAdmin (read-only, revoke action)
backend/config/settings/base.py                 + MCP_SERVER_ENABLED
backend/config/settings/test.py                 + MCP_SERVER_ENABLED = True
backend/config/urls.py                          + mcp/ and api/v1/mcp-tokens/
backend/apps/common/logging.py                  + fjp_ token pattern
backend/apps/common/verify/mcp.py               verify_platform module
backend/apps/common/verify/__init__.py          register "mcp" before "logout"
backend/tests/tenancy/mcp/{__init__,test_models,test_tokens_api,test_auth,test_rpc,test_ratelimit,test_tools_jobs,test_tools_applications}.py
deploy/nginx.conf                               + location ^~ /mcp/
web/api.js                                      + mcpTokens endpoint + client fns
web/app.html                                    Settings → "Connected assistants"
```

Models live in `apps/tenancy/mcp/models.py` but are **registered under the `tenancy` app** (the `mcp` package is not an `AppConfig`; `apps/tenancy/models.py` imports it at the bottom so Django discovers it, like a split-models module). This keeps one migration history and avoids another `INSTALLED_APPS` entry.

---

### Task 1: McpToken model, migration, admin

**Files:**
- Create: `backend/apps/tenancy/mcp/__init__.py` (empty)
- Create: `backend/apps/tenancy/mcp/models.py`
- Modify: `backend/apps/tenancy/models.py` (append import at end of file)
- Create: `backend/apps/tenancy/migrations/0002_mcptoken.py` (via makemigrations)
- Modify: `backend/apps/tenancy/admin.py`
- Test: `backend/tests/tenancy/mcp/__init__.py` (empty), `backend/tests/tenancy/mcp/test_models.py`

**Interfaces:**
- Produces: `SCOPES: tuple[str, ...]`, `TOKEN_PREFIX = "fjp_"`, `TOKEN_LENGTH = 44`, `PREFIX_LENGTH = 12`, `generate_token() -> tuple[str, str, str]` (plaintext, prefix, sha256 hex), `hash_token(raw: str) -> str`, `McpToken` with fields `org, user, name, prefix, token_hash, scopes, expires_at, last_used_at, last_used_ip, revoked_at, created_at`, methods `is_usable(now=None) -> bool`, `has_scope(scope) -> bool`, `status_code() -> str` (`"active"|"expired"|"revoked"`), managers `objects` (OrgScopedManager user_field="user") and `all_objects`.

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/tenancy/mcp/test_models.py
"""McpToken — spec §4 phase 1."""
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.tenancy.managers import OrgScopeError
from apps.tenancy.mcp.models import (
    PREFIX_LENGTH, SCOPES, TOKEN_LENGTH, TOKEN_PREFIX, McpToken,
    generate_token, hash_token,
)
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user


class GenerateTokenTests(TestCase):
    def test_shape(self):
        raw, prefix, digest = generate_token()
        self.assertTrue(raw.startswith(TOKEN_PREFIX))
        self.assertEqual(len(raw), TOKEN_LENGTH)
        self.assertEqual(prefix, raw[:PREFIX_LENGTH])
        self.assertEqual(digest, hash_token(raw))
        self.assertEqual(len(digest), 64)

    def test_two_tokens_differ(self):
        self.assertNotEqual(generate_token()[0], generate_token()[0])


class McpTokenModelTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.membership = bootstrap_workspace(self.user)
        self.org = self.membership.org

    def _make(self, **overrides) -> McpToken:
        raw, prefix, digest = generate_token()
        fields = dict(org=self.org, user=self.user, name="laptop",
                      prefix=prefix, token_hash=digest, scopes=list(SCOPES))
        fields.update(overrides)
        return McpToken.objects.create(**fields)

    def test_unscoped_read_raises(self):
        self._make()
        with self.assertRaises(OrgScopeError):
            list(McpToken.objects.all())

    def test_for_org_reads(self):
        token = self._make()
        self.assertEqual(list(McpToken.objects.for_org(self.org)), [token])

    def test_is_usable_states(self):
        now = timezone.now()
        self.assertTrue(self._make().is_usable(now))
        self.assertFalse(self._make(expires_at=now - timedelta(seconds=1)).is_usable(now))
        self.assertFalse(self._make(revoked_at=now).is_usable(now))
        self.assertEqual(self._make(revoked_at=now).status_code(), "revoked")
        self.assertEqual(self._make(expires_at=now - timedelta(days=1)).status_code(), "expired")
        self.assertEqual(self._make().status_code(), "active")

    def test_has_scope(self):
        token = self._make(scopes=["jobs:read"])
        self.assertTrue(token.has_scope("jobs:read"))
        self.assertFalse(token.has_scope("applications:send"))

    def test_token_hash_is_unique(self):
        token = self._make()
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            McpToken.objects.create(org=self.org, user=self.user, name="dup",
                                    prefix=token.prefix, token_hash=token.token_hash,
                                    scopes=[])
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_models.py -q`
Expected: ImportError on `apps.tenancy.mcp.models`.

- [ ] **Step 3: Write the model**

```python
# backend/apps/tenancy/mcp/models.py
"""McpToken — personal access tokens for the MCP endpoint (spec §4, phase 1).

Registered under the ``tenancy`` app: ``apps/tenancy/models.py`` imports this
module at its end so Django discovers the model in the tenancy migration
history. Plaintext is shown once at creation; only ``token_hash`` is stored.
"""
from __future__ import annotations

import hashlib
import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.tenancy.managers import OrgScopedManager

SCOPES: tuple[str, ...] = (
    "jobs:read",
    "applications:read",
    "applications:write",
    "applications:send",
    "profile:read",
)
TOKEN_PREFIX = "fjp_"
#: ``fjp_`` + 40 url-safe characters.
TOKEN_LENGTH = 44
#: ``fjp_`` + first 8 random characters; shown in the UI, used for lookup.
PREFIX_LENGTH = 12
DEFAULT_TTL_DAYS = 90


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def generate_token() -> tuple[str, str, str]:
    """Return ``(plaintext, prefix, sha256_hex)``."""
    body = secrets.token_urlsafe(30)[:40]
    while len(body) < 40:  # token_urlsafe(30) yields 40 chars; belt and braces
        body += secrets.token_urlsafe(8)
        body = body[:40]
    raw = TOKEN_PREFIX + body
    return raw, raw[:PREFIX_LENGTH], hash_token(raw)


class McpToken(models.Model):
    org = models.ForeignKey(
        "tenancy.Organization", on_delete=models.CASCADE,
        related_name="mcp_tokens", verbose_name=_("organization"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="mcp_tokens", verbose_name=_("user"),
    )
    name = models.CharField(_("name"), max_length=100)
    prefix = models.CharField(_("prefix"), max_length=PREFIX_LENGTH, db_index=True)
    token_hash = models.CharField(_("token hash"), max_length=64, unique=True)
    scopes = models.JSONField(_("scopes"), default=list)
    expires_at = models.DateTimeField(_("expires at"), null=True, blank=True)
    last_used_at = models.DateTimeField(_("last used at"), null=True, blank=True)
    last_used_ip = models.GenericIPAddressField(_("last used IP"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    objects = OrgScopedManager(user_field="user")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("MCP token")
        verbose_name_plural = _("MCP tokens")
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:  # pragma: no cover - admin nicety
        return f"{self.prefix}… ({self.name})"

    def is_usable(self, now=None) -> bool:
        now = now or timezone.now()
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now

    def status_code(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if self.expires_at is not None and self.expires_at <= timezone.now():
            return "expired"
        return "active"

    def has_scope(self, scope: str) -> bool:
        return scope in (self.scopes or [])
```

Append to the **end** of `backend/apps/tenancy/models.py`:

```python
# Split-models module: McpToken lives in apps/tenancy/mcp/models.py but belongs
# to this app's migration history. The import must stay at the bottom so the
# mcp module can import OrgScopedManager without a cycle.
from apps.tenancy.mcp.models import McpToken  # noqa: E402,F401
```

- [ ] **Step 4: Make the migration**

Run: `cd backend && DJANGO_SETTINGS_MODULE=config.settings.test ../.venv/bin/python manage.py makemigrations tenancy -n mcptoken`
Expected: `apps/tenancy/migrations/0002_mcptoken.py` created. Open it and confirm the model name is `McpToken` and the app is `tenancy`.

- [ ] **Step 5: Run tests**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_models.py -q`
Expected: 6 passed.

- [ ] **Step 6: Admin registration**

Append to `backend/apps/tenancy/admin.py` (after the existing registrations; reuse the mixins imported at the top of that file):

```python
from django.utils import timezone as _tz

from apps.tenancy.mcp.models import McpToken


@admin.register(McpToken)
class McpTokenAdmin(UnguardedQuerySetMixin, ReadOnlyAdminMixin, admin.ModelAdmin):
    """Operator view of MCP tokens. Hash never shown; revoke is the one action."""

    list_display = ("prefix", "name", "user", "org", "scopes", "expires_at",
                    "last_used_at", "revoked_at")
    list_filter = ("org",)
    search_fields = ("prefix", "name", "user__email")
    readonly_fields = ("prefix", "name", "user", "org", "scopes", "expires_at",
                       "last_used_at", "last_used_ip", "revoked_at", "created_at")
    exclude = ("token_hash",)
    actions = ("revoke",)

    @admin.action(description=_("Revoke selected tokens"))
    def revoke(self, request, queryset):
        queryset.filter(revoked_at__isnull=True).update(revoked_at=_tz.now())
```

If `ReadOnlyAdminMixin` blocks actions in this codebase (check `apps/common/admin_support.py`), override `has_change_permission` to return `True` only for the action path as the mixin documents; otherwise leave as above.

- [ ] **Step 7: Run the whole suite and commit**

Run: `cd backend && ../.venv/bin/pytest -q`
Expected: all green (the admin smoke tests in `tests/admin/` load every registered admin).

```bash
git add backend/apps/tenancy/mcp/__init__.py backend/apps/tenancy/mcp/models.py backend/apps/tenancy/models.py backend/apps/tenancy/migrations/0002_mcptoken.py backend/apps/tenancy/admin.py backend/tests/tenancy/mcp/
git commit -m "feat(mcp): Add McpToken model, migration and admin

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Token management API `/api/v1/mcp-tokens/`

**Files:**
- Create: `backend/apps/tenancy/mcp/token_serializers.py`, `backend/apps/tenancy/mcp/token_views.py`, `backend/apps/tenancy/mcp/token_urls.py`
- Modify: `backend/config/urls.py` (inside `api_v1_patterns`)
- Test: `backend/tests/tenancy/mcp/test_tokens_api.py`

**Interfaces:**
- Consumes: `McpToken`, `generate_token`, `SCOPES`, `DEFAULT_TTL_DAYS` from Task 1; `IsOrgMember`, `current_org`, `current_membership`, `AuditLog.objects.record`.
- Produces: `GET /api/v1/mcp-tokens/` → list of `{id, name, prefix, scopes, expires_at, last_used_at, status, created_at}`; `POST` body `{name, scopes, expires_in_days?}` → 201 same shape + `token`; `DELETE /api/v1/mcp-tokens/<id>/` → 204. Audit actions `mcp_token.created`, `mcp_token.revoked`.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/tenancy/mcp/test_tokens_api.py
from __future__ import annotations

from django.test import TestCase
from rest_framework.test import APIClient

from apps.tenancy.mcp.models import McpToken
from apps.tenancy.models import AuditLog
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user

URL = "/api/v1/mcp-tokens/"
BODY = {"name": "Claude Desktop", "scopes": ["jobs:read", "applications:read"]}


class AnonymousTests(TestCase):
    def test_401_envelope(self):
        r = APIClient().get(URL)
        self.assertEqual(r.status_code, 401)
        self.assertIn("error", r.json())


class TokenApiTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.client = APIClient()
        self.client.force_login(self.user)

    def test_create_returns_plaintext_once_and_stores_hash_only(self):
        r = self.client.post(URL, BODY, format="json")
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertTrue(data["token"].startswith("fjp_"))
        self.assertEqual(len(data["token"]), 44)
        self.assertEqual(data["prefix"], data["token"][:12])
        self.assertEqual(data["scopes"], BODY["scopes"])
        self.assertIsNotNone(data["expires_at"])
        row = McpToken.all_objects.get()
        self.assertNotEqual(row.token_hash, data["token"])
        listed = self.client.get(URL).json()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        self.assertNotIn("token_hash", listed[0])
        self.assertEqual(listed[0]["status"], "active")

    def test_unknown_scope_is_400(self):
        r = self.client.post(URL, {"name": "x", "scopes": ["admin:all"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid")

    def test_empty_scopes_is_400(self):
        r = self.client.post(URL, {"name": "x", "scopes": []}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_expires_in_days_bounds(self):
        self.assertEqual(self.client.post(URL, dict(BODY, expires_in_days=0), format="json").status_code, 400)
        self.assertEqual(self.client.post(URL, dict(BODY, expires_in_days=366), format="json").status_code, 400)
        self.assertEqual(self.client.post(URL, dict(BODY, expires_in_days=30), format="json").status_code, 201)

    def test_revoke_is_soft_and_audited(self):
        token_id = self.client.post(URL, BODY, format="json").json()["id"]
        r = self.client.delete(f"{URL}{token_id}/")
        self.assertEqual(r.status_code, 204)
        row = McpToken.all_objects.get(pk=token_id)
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(self.client.get(URL).json()[0]["status"], "revoked")
        actions = set(AuditLog.objects.for_org(self.org).values_list("action", flat=True))
        self.assertEqual(actions, {"mcp_token.created", "mcp_token.revoked"})

    def test_cannot_revoke_another_members_token(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        foreign = McpToken.objects.create(org=other_org, user=other, name="f",
                                          prefix="fjp_aaaaaaaa", token_hash="0" * 64,
                                          scopes=["jobs:read"])
        r = self.client.delete(f"{URL}{foreign.pk}/")
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(McpToken.all_objects.get(pk=foreign.pk).revoked_at)
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_tokens_api.py -q`
Expected: 404s / failures (route absent).

- [ ] **Step 3: Serializers**

```python
# backend/apps/tenancy/mcp/token_serializers.py
from __future__ import annotations

from rest_framework import serializers

from apps.tenancy.mcp.models import DEFAULT_TTL_DAYS, SCOPES, McpToken


class McpTokenSerializer(serializers.ModelSerializer):
    status = serializers.CharField(source="status_code", read_only=True)

    class Meta:
        model = McpToken
        fields = ("id", "name", "prefix", "scopes", "expires_at",
                  "last_used_at", "status", "created_at")
        read_only_fields = fields


class CreateMcpTokenSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=SCOPES), allow_empty=False,
    )
    expires_in_days = serializers.IntegerField(
        min_value=1, max_value=365, default=DEFAULT_TTL_DAYS,
    )

    def validate_scopes(self, value):
        return sorted(set(value), key=SCOPES.index)
```

- [ ] **Step 4: Views and URLs**

```python
# backend/apps/tenancy/mcp/token_views.py
"""``/api/v1/mcp-tokens/`` — session-authenticated management of MCP PATs."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.errors import ApiError, ErrorCode
from apps.tenancy.mcp.models import McpToken, generate_token
from apps.tenancy.mcp.token_serializers import CreateMcpTokenSerializer, McpTokenSerializer
from apps.tenancy.models import AuditLog
from apps.tenancy.permissions import IsOrgMember
from apps.tenancy.services import current_org


def _own_tokens(request: Request):
    return McpToken.objects.for_org(current_org(request)).filter(user=request.user)


class McpTokenListView(APIView):
    permission_classes = [IsOrgMember]

    def get(self, request: Request) -> Response:
        return Response(McpTokenSerializer(_own_tokens(request), many=True).data)

    def post(self, request: Request) -> Response:
        serializer = CreateMcpTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        raw, prefix, digest = generate_token()
        org = current_org(request)
        token = McpToken.objects.create(
            org=org, user=request.user, name=data["name"], prefix=prefix,
            token_hash=digest, scopes=data["scopes"],
            expires_at=timezone.now() + timedelta(days=data["expires_in_days"]),
        )
        AuditLog.objects.record(
            action="mcp_token.created", org=org, actor=request.user, target=token,
            payload={"prefix": prefix, "scopes": data["scopes"]},
            ip=request.META.get("REMOTE_ADDR"),
        )
        body = McpTokenSerializer(token).data
        body["token"] = raw  # the one and only time the plaintext leaves the server
        return Response(body, status=status.HTTP_201_CREATED)


class McpTokenDetailView(APIView):
    permission_classes = [IsOrgMember]

    def delete(self, request: Request, token_id: int) -> Response:
        token = _own_tokens(request).filter(pk=token_id).first()
        if token is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Token not found.", status.HTTP_404_NOT_FOUND)
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])
            AuditLog.objects.record(
                action="mcp_token.revoked", org=token.org, actor=request.user,
                target=token, payload={"prefix": token.prefix},
                ip=request.META.get("REMOTE_ADDR"),
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
```

```python
# backend/apps/tenancy/mcp/token_urls.py
from django.urls import path

from apps.tenancy.mcp.token_views import McpTokenDetailView, McpTokenListView

app_name = "mcp_tokens"

urlpatterns = [
    path("", McpTokenListView.as_view(), name="list"),
    path("<int:token_id>/", McpTokenDetailView.as_view(), name="detail"),
]
```

In `backend/config/urls.py`, inside `api_v1_patterns`, after the outbox line:

```python
    # apps.tenancy.mcp — personal access tokens for the MCP endpoint (/mcp/).
    *_include_if_present("apps.tenancy.mcp.token_urls", "mcp-tokens/"),
```

Also add a row to the route-ownership table in that module's docstring:
`` ``/api/v1/mcp-tokens/…``  apps.tenancy.mcp  session ``.

- [ ] **Step 5: Run tests, then the suite, commit**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_tokens_api.py -q && ../.venv/bin/pytest -q`
Expected: all pass. If `test_project_contract.py` pins the endpoint list, add `mcp-tokens/` there as the failure message instructs.

```bash
git add backend/apps/tenancy/mcp/token_*.py backend/config/urls.py backend/tests/tenancy/mcp/test_tokens_api.py
git commit -m "feat(mcp): Session-authenticated PAT management at /api/v1/mcp-tokens/

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Bearer auth + JSON-RPC endpoint skeleton at `/mcp/`

**Files:**
- Create: `backend/apps/tenancy/mcp/auth.py`, `rpc.py`, `tools.py`, `views.py`, `urls.py`
- Modify: `backend/config/settings/base.py`, `backend/config/settings/test.py`, `backend/config/urls.py`, `backend/apps/common/logging.py`
- Test: `backend/tests/tenancy/mcp/test_auth.py`, `backend/tests/tenancy/mcp/test_rpc.py`, `backend/tests/common/test_logging.py` (append one test; create if absent)

**Interfaces:**
- Produces:
  - `auth.McpAuthError(code: str, detail: str)`; `auth.authenticate(request) -> tuple[McpToken, Membership]`.
  - `tools.ToolError(code: str, detail: str)`; `tools.ToolContext(token, membership, org, user, ip)`; `tools.Tool(name, description, scope, input_schema, handler)`; `tools.REGISTRY: dict[str, Tool]`; `tools.register(name, *, description, scope, input_schema)` decorator; `tools.tool_ok(data: dict, text: str) -> dict`; `tools.tool_error(code, detail) -> dict`.
  - `rpc.dispatch(body: bytes, ctx: ToolContext) -> tuple[int, dict | None]` (HTTP status, JSON body; `None` body for notifications → 202).
  - `views.McpEndpoint`.
  - Test helper `tests/tenancy/mcp/support.py`: `make_token(user, org, scopes=SCOPES) -> tuple[str, McpToken]`, `rpc(client, raw_token, method, params=None, id=1) -> HttpResponse`.

- [ ] **Step 1: Test helper**

```python
# backend/tests/tenancy/mcp/support.py
from __future__ import annotations

import json

from apps.tenancy.mcp.models import SCOPES, McpToken, generate_token

MCP_URL = "/mcp/"


def make_token(user, org, scopes=SCOPES, **overrides) -> tuple[str, McpToken]:
    raw, prefix, digest = generate_token()
    fields = dict(org=org, user=user, name="test", prefix=prefix,
                  token_hash=digest, scopes=list(scopes))
    fields.update(overrides)
    return raw, McpToken.objects.create(**fields)


def rpc(client, raw_token, method, params=None, id=1, *, raw_body=None):
    body = raw_body if raw_body is not None else json.dumps(
        {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
    )
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw_token}"} if raw_token else {}
    return client.post(MCP_URL, data=body, content_type="application/json", **headers)


def call_tool(client, raw_token, name, arguments=None, id=1):
    return rpc(client, raw_token, "tools/call",
               {"name": name, "arguments": arguments or {}}, id=id)
```

- [ ] **Step 2: Failing auth tests**

```python
# backend/tests/tenancy/mcp/test_auth.py
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.tenancy.models import Membership
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user
from tests.tenancy.mcp.support import MCP_URL, make_token, rpc


class McpAuthTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.membership = bootstrap_workspace(self.user)
        self.org = self.membership.org
        self.raw, self.token = make_token(self.user, self.org)

    def _assert_401(self, response, code):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")
        self.assertEqual(response.json()["error"]["code"], code)

    def test_missing_header(self):
        self._assert_401(rpc(self.client, None, "ping"), "token_missing")

    def test_garbage_token(self):
        self._assert_401(rpc(self.client, "nope", "ping"), "token_invalid")

    def test_wrong_tail_same_prefix_is_401(self):
        forged = self.raw[:12] + "x" * 32
        self._assert_401(rpc(self.client, forged, "ping"), "token_invalid")

    def test_expired(self):
        self.token.expires_at = timezone.now() - timedelta(seconds=1)
        self.token.save()
        self._assert_401(rpc(self.client, self.raw, "ping"), "token_expired")

    def test_revoked(self):
        self.token.revoked_at = timezone.now()
        self.token.save()
        self._assert_401(rpc(self.client, self.raw, "ping"), "token_revoked")

    def test_membership_removed_kills_token(self):
        Membership.all_objects.filter(pk=self.membership.pk).delete()
        self._assert_401(rpc(self.client, self.raw, "ping"), "token_invalid")

    def test_session_cookie_alone_is_rejected(self):
        self.client.force_login(self.user)
        self._assert_401(rpc(self.client, None, "ping"), "token_missing")

    def test_valid_token_pings_and_touches_last_used(self):
        r = rpc(self.client, self.raw, "ping")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)

    @override_settings(MCP_SERVER_ENABLED=False)
    def test_feature_flag_off_is_404_envelope(self):
        r = rpc(self.client, self.raw, "ping")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

    def test_get_is_405_and_delete_is_204(self):
        self.assertEqual(self.client.get(MCP_URL).status_code, 405)
        r = self.client.delete(MCP_URL, HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(r.status_code, 204)
```

- [ ] **Step 3: Failing RPC tests**

```python
# backend/tests/tenancy/mcp/test_rpc.py
from __future__ import annotations

import json

from django.test import TestCase

from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user
from tests.tenancy.mcp.support import MCP_URL, make_token, rpc


class RpcEnvelopeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.raw, self.token = make_token(self.user, self.org)

    def test_initialize(self):
        r = rpc(self.client, self.raw, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(r.status_code, 200)
        result = r.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "findajob.pro")
        self.assertIn("tools", result["capabilities"])
        self.assertNotIn("resources", result["capabilities"])

    def test_initialized_notification_is_202_empty(self):
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = rpc(self.client, self.raw, None, raw_body=body)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.content, b"")

    def test_unknown_method(self):
        r = rpc(self.client, self.raw, "resources/list")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"]["code"], -32601)

    def test_batch_rejected(self):
        r = rpc(self.client, self.raw, None, raw_body="[]")
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_parse_error(self):
        r = rpc(self.client, self.raw, None, raw_body="{not json")
        self.assertEqual(r.json()["error"]["code"], -32700)

    def test_missing_jsonrpc_version(self):
        r = rpc(self.client, self.raw, None, raw_body=json.dumps({"id": 1, "method": "ping"}))
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_body_over_256kb_is_413_envelope(self):
        r = rpc(self.client, self.raw, None, raw_body="x" * (256 * 1024 + 1))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["error"]["code"], "payload_too_large")

    def test_tools_list_shape(self):
        r = rpc(self.client, self.raw, "tools/list")
        tools = r.json()["result"]["tools"]
        self.assertIsInstance(tools, list)
        for tool in tools:
            self.assertEqual(set(tool) >= {"name", "description", "inputSchema"}, True)

    def test_tools_call_unknown_tool(self):
        r = rpc(self.client, self.raw, "tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(r.json()["error"]["code"], -32602)

    def test_tools_call_missing_scope(self):
        raw, _ = make_token(self.user, self.org, scopes=["applications:read"])
        r = rpc(self.client, raw, "tools/call", {"name": "list_jobs", "arguments": {}})
        result = r.json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "scope_missing")
```

The last test needs `list_jobs` registered; it passes after Task 5. Mark it `@skip("Task 5")` for now and remove the skip in Task 5.

- [ ] **Step 4: Run to verify failure**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_auth.py tests/tenancy/mcp/test_rpc.py -q`
Expected: all fail (no route).

- [ ] **Step 5: Settings and logging**

`backend/config/settings/base.py`, near the other feature switches (after `TRUSTED_PROXY_COUNT`):

```python
# MCP server (docs/superpowers/specs/2026-09-26-mcp-server-design.md). Off by
# default until nginx carries the /mcp/ route on the target.
MCP_SERVER_ENABLED = env.get_bool("MCP_SERVER_ENABLED", False)
```

`backend/config/settings/test.py`, at the end: `MCP_SERVER_ENABLED = True`.

`backend/apps/common/logging.py`: add after `_BARE_OTP`:

```python
#: An MCP personal access token anywhere in a log line.
_MCP_TOKEN = re.compile(r"fjp_[A-Za-z0-9_-]{40}")
```

and in `redact()` before the final `return`: `text = _MCP_TOKEN.sub(REDACTED, text)`.

Append to `backend/tests/common/test_logging.py` (create the file with the standard header if it does not exist):

```python
def test_mcp_token_is_redacted():
    from apps.common.logging import REDACTED, redact
    raw = "fjp_" + "A" * 40
    assert raw not in redact(f"token seen {raw} in body")
    assert REDACTED in redact(f"token seen {raw} in body")
```

- [ ] **Step 6: auth.py**

```python
# backend/apps/tenancy/mcp/auth.py
"""Bearer authentication for ``/mcp/`` (spec §4).

No session fallback, ever: a browser cookie must not drive a CSRF-exempt
endpoint. Lookup is by prefix, then a constant-time hash compare.
"""
from __future__ import annotations

import hmac

from django.utils import timezone

from apps.tenancy.mcp.models import PREFIX_LENGTH, TOKEN_LENGTH, TOKEN_PREFIX, McpToken, hash_token
from apps.tenancy.models import Membership

#: Update ``last_used_at`` at most this often per token (spec §4).
TOUCH_INTERVAL_SECONDS = 60


class McpAuthError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _client_ip(request) -> str | None:
    return request.META.get("REMOTE_ADDR") or None


def authenticate(request) -> tuple[McpToken, Membership]:
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, raw = header.partition(" ")
    raw = raw.strip()
    if scheme.lower() != "bearer" or not raw:
        raise McpAuthError("token_missing", "Send Authorization: Bearer <token>.")
    if not raw.startswith(TOKEN_PREFIX) or len(raw) != TOKEN_LENGTH:
        raise McpAuthError("token_invalid", "The token is not valid.")

    # Pre-auth lookup: there is no org yet, so this is the one blessed
    # unscoped read in the package.
    token = (
        McpToken.objects.unscoped()
        .select_related("org", "user")
        .filter(prefix=raw[:PREFIX_LENGTH])
        .first()
    )
    if token is None or not hmac.compare_digest(token.token_hash, hash_token(raw)):
        raise McpAuthError("token_invalid", "The token is not valid.")

    now = timezone.now()
    if token.revoked_at is not None:
        raise McpAuthError("token_revoked", "The token was revoked.")
    if token.expires_at is not None and token.expires_at <= now:
        raise McpAuthError("token_expired", "The token has expired.")

    membership = (
        Membership.objects.for_org(token.org).filter(user=token.user).first()
    )
    if membership is None:
        raise McpAuthError("token_invalid", "The token is not valid.")

    if token.last_used_at is None or (now - token.last_used_at).total_seconds() >= TOUCH_INTERVAL_SECONDS:
        token.last_used_at = now
        token.last_used_ip = _client_ip(request)
        token.save(update_fields=["last_used_at", "last_used_ip"])
    return token, membership
```

Check `Membership.objects.for_org(...).filter(user=...)` is how `tests/tenancy/test_org_scoping.py` reads memberships; if the queryset there uses `for_user(user)` + `.filter(org=...)`, mirror that instead.

- [ ] **Step 7: tools.py (registry only)**

```python
# backend/apps/tenancy/mcp/tools.py
"""Tool registry for the MCP endpoint (spec §5/§6).

Handlers are ``(ctx, arguments) -> dict`` and may raise :class:`ToolError`.
Argument validation is done by each handler with a DRF serializer; the
``input_schema`` here is what ``tools/list`` advertises.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from rest_framework import serializers

Handler = Callable[["ToolContext", dict[str, Any]], dict[str, Any]]


class ToolError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ToolContext:
    token: Any
    membership: Any
    org: Any
    user: Any
    ip: str | None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    scope: str
    input_schema: dict[str, Any]
    handler: Handler

    def descriptor(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


REGISTRY: dict[str, Tool] = {}


def register(name: str, *, description: str, scope: str, input_schema: dict[str, Any]):
    def decorator(fn: Handler) -> Handler:
        REGISTRY[name] = Tool(name, description, scope, input_schema, fn)
        return fn
    return decorator


def validate(serializer_class: type[serializers.Serializer], arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a DRF serializer over tool arguments; map failure to ``invalid_arguments``."""
    serializer = serializer_class(data=arguments)
    if not serializer.is_valid():
        first = next(iter(serializer.errors.items()))
        raise ToolError("invalid_arguments", f"{first[0]}: {first[1][0]}")
    return serializer.validated_data


def tool_ok(data: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text or json.dumps(data, default=str)}],
        "structuredContent": data,
        "isError": False,
    }


def tool_error(code: str, detail: str) -> dict[str, Any]:
    payload = {"error": {"code": code, "detail": detail}}
    return {
        "content": [{"type": "text", "text": f"{code}: {detail}"}],
        "structuredContent": payload,
        "isError": True,
    }


def load_all() -> None:
    """Import the handler modules so their ``@register`` calls run."""
    from apps.tenancy.mcp import tools_applications, tools_jobs  # noqa: F401
```

Until Tasks 5–7 exist, make `load_all()` tolerate missing modules:

```python
def load_all() -> None:
    import importlib
    for name in ("tools_jobs", "tools_applications"):
        try:
            importlib.import_module(f"apps.tenancy.mcp.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"apps.tenancy.mcp.{name}":
                raise
```

Keep this tolerant form; it mirrors `_include_if_present` in `config/urls.py`.

- [ ] **Step 8: rpc.py**

```python
# backend/apps/tenancy/mcp/rpc.py
"""JSON-RPC 2.0 envelope for MCP Streamable HTTP, phase 1 (spec §3).

Single request per POST (no batches), plain JSON responses (no SSE).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from django.db import transaction

from apps.tenancy.mcp.tools import REGISTRY, ToolContext, ToolError, load_all, tool_error

logger = logging.getLogger("findajob.mcp")

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "findajob.pro", "version": "1"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _initialize(params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": (
            "findajob.pro job pipeline for this member's workspace. Drafts go to "
            "REVIEW; sending is a separate explicit tool. LinkedIn, Indeed and "
            "Wellfound are never automated: use mark_sent_manually after the "
            "member has applied by hand."
        ),
    }


def _tools_list(params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    load_all()
    return {"tools": [tool.descriptor() for tool in REGISTRY.values()]}


def _tools_call(params: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
    load_all()
    name = params.get("name")
    tool = REGISTRY.get(name or "")
    if tool is None:
        return None  # caller maps to INVALID_PARAMS
    if not ctx.token.has_scope(tool.scope):
        return tool_error("scope_missing", f"This token lacks the {tool.scope} scope.")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return tool_error("invalid_arguments", "arguments must be an object.")
    try:
        with transaction.atomic():
            return tool.handler(ctx, arguments)
    except ToolError as exc:
        return tool_error(exc.code, exc.detail)
    except Exception:  # noqa: BLE001 - every tool failure must stay in-band
        logger.exception("MCP tool %s failed", name)
        return tool_error("server_error", "The tool failed unexpectedly.")


def dispatch(body: bytes, ctx: ToolContext) -> tuple[int, dict[str, Any] | None]:
    """Return ``(http_status, json_body_or_None)``."""
    try:
        message = json.loads(body)
    except ValueError:
        return 200, _error(None, PARSE_ERROR, "Parse error")
    if not isinstance(message, dict):
        return 200, _error(None, INVALID_REQUEST, "Batch requests are not supported")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return 200, _error(message.get("id"), INVALID_REQUEST, "Invalid Request")

    method = message["method"]
    params = message.get("params") or {}
    is_notification = "id" not in message
    id_ = message.get("id")

    if method.startswith("notifications/"):
        return 202, None
    if method == "ping":
        result: Any = {}
    elif method == "initialize":
        result = _initialize(params, ctx)
    elif method == "tools/list":
        result = _tools_list(params, ctx)
    elif method == "tools/call":
        result = _tools_call(params, ctx)
        if result is None:
            return 200, _error(id_, INVALID_PARAMS, "Unknown tool")
    else:
        return 200, _error(id_, METHOD_NOT_FOUND, "Method not found")

    if is_notification:
        return 202, None
    return 200, _result(id_, result)
```

- [ ] **Step 9: views.py and urls.py**

```python
# backend/apps/tenancy/mcp/views.py
"""``/mcp/`` — the MCP Streamable HTTP endpoint (spec §3).

A plain Django view on purpose: no DRF authentication classes, no session,
no CSRF. Bearer only (``auth.authenticate``). Failures before dispatch use the
project envelope; failures inside dispatch are JSON-RPC.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.common.errors import ErrorCode, envelope
from apps.tenancy.mcp import rpc
from apps.tenancy.mcp.auth import McpAuthError, authenticate
from apps.tenancy.mcp.ratelimit import McpRateLimited, check_budget
from apps.tenancy.mcp.tools import ToolContext

MAX_BODY_BYTES = 256 * 1024


def _envelope(status: int, code: str, detail: str, **headers: str) -> JsonResponse:
    response = JsonResponse(envelope(code, detail), status=status)
    for key, value in headers.items():
        response[key.replace("_", "-")] = value
    return response


@method_decorator(csrf_exempt, name="dispatch")
class McpEndpoint(View):
    http_method_names = ["post", "delete", "options"]

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if not settings.MCP_SERVER_ENABLED:
            return _envelope(404, ErrorCode.NOT_FOUND, "Not found.")
        return super().dispatch(request, *args, **kwargs)

    def delete(self, request: HttpRequest) -> HttpResponse:
        try:
            authenticate(request)
        except McpAuthError as exc:
            return _envelope(401, exc.code, exc.detail, WWW_Authenticate="Bearer")
        return HttpResponse(status=204)

    def post(self, request: HttpRequest) -> HttpResponse:
        try:
            token, membership = authenticate(request)
        except McpAuthError as exc:
            return _envelope(401, exc.code, exc.detail, WWW_Authenticate="Bearer")
        if len(request.body) > MAX_BODY_BYTES:
            return _envelope(413, "payload_too_large", "Request body exceeds 256 KB.")
        try:
            check_budget(token.pk)
        except McpRateLimited as exc:
            return _envelope(429, ErrorCode.RATE_LIMITED,
                             "Too many requests. Please try again later.",
                             Retry_After=str(exc.retry_after))
        ctx = ToolContext(token=token, membership=membership, org=membership.org,
                          user=token.user, ip=request.META.get("REMOTE_ADDR"))
        status, body = rpc.dispatch(request.body, ctx)
        if body is None:
            return HttpResponse(status=status)
        return JsonResponse(body, status=status)
```

Note: `http_method_names` excludes `get`, so Django's `View` answers 405 automatically. Confirm the 405 body is acceptable (Django's default is empty); the test only checks the status.

```python
# backend/apps/tenancy/mcp/urls.py
from django.urls import path

from apps.tenancy.mcp.views import McpEndpoint

app_name = "mcp"

urlpatterns = [path("", McpEndpoint.as_view(), name="endpoint")]
```

`backend/config/urls.py`, in `urlpatterns` after the email webhooks line:

```python
    # MCP Streamable HTTP endpoint. Outside /api/: bearer-only, CSRF-exempt,
    # JSON-RPC. See docs/superpowers/specs/2026-09-26-mcp-server-design.md.
    *_include_if_present("apps.tenancy.mcp.urls", "mcp/"),
```

- [ ] **Step 10: Stub ratelimit so the view imports** (real implementation in Task 4)

```python
# backend/apps/tenancy/mcp/ratelimit.py
from __future__ import annotations


class McpRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = max(1, int(retry_after))


def check_budget(token_id: int) -> None:
    return None
```

- [ ] **Step 11: Run tests, suite, commit**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp tests/common -q && ../.venv/bin/pytest -q`
Expected: all pass except the one skipped scope test.

```bash
git add backend/apps/tenancy/mcp backend/config backend/apps/common/logging.py backend/tests
git commit -m "feat(mcp): Bearer-authenticated JSON-RPC endpoint at /mcp/

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Per-token rate limit, fail closed

**Files:**
- Modify: `backend/apps/tenancy/mcp/ratelimit.py`
- Test: `backend/tests/tenancy/mcp/test_ratelimit.py`

**Interfaces:**
- Produces: `check_budget(token_id: int) -> None`, raises `McpRateLimited(retry_after)`. Constants `PER_MINUTE = 60`, `PER_HOUR = 600`.

- [ ] **Step 1: Failing tests**

```python
# backend/tests/tenancy/mcp/test_ratelimit.py
from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from apps.tenancy.mcp.ratelimit import PER_MINUTE, McpRateLimited, check_budget
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user
from tests.tenancy.mcp.support import make_token, rpc


class BudgetTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_allows_up_to_limit_then_rejects(self):
        for _ in range(PER_MINUTE):
            check_budget(1)
        with self.assertRaises(McpRateLimited) as ctx:
            check_budget(1)
        self.assertGreaterEqual(ctx.exception.retry_after, 1)

    def test_tokens_are_independent(self):
        for _ in range(PER_MINUTE):
            check_budget(1)
        check_budget(2)

    def test_cache_failure_fails_closed(self):
        with mock.patch("apps.tenancy.mcp.ratelimit.cache.add", side_effect=RuntimeError("redis down")):
            with self.assertRaises(McpRateLimited):
                check_budget(3)


class BudgetHttpTests(TestCase):
    def test_429_envelope_with_retry_after(self):
        cache.clear()
        user = make_user()
        org = bootstrap_workspace(user).org
        raw, token = make_token(user, org)
        with mock.patch("apps.tenancy.mcp.views.check_budget", side_effect=McpRateLimited(17)):
            r = rpc(self.client, raw, "ping")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"]["code"], "rate_limited")
        self.assertEqual(r["Retry-After"], "17")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_ratelimit.py -q`
Expected: first and third tests fail (stub never raises).

- [ ] **Step 3: Implement**

```python
# backend/apps/tenancy/mcp/ratelimit.py
"""Per-token budget for ``tools/call`` and friends (spec §7).

Fixed windows, two of them. **Fails closed**: if the cache cannot answer, the
assistant is told to retry later while the browser is unaffected — the same
policy as the OTP verify budget, and for the same reason: an unknown budget
must not become an unlimited one.
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache

logger = logging.getLogger("findajob.mcp")

PER_MINUTE = 60
PER_HOUR = 600
_WINDOWS = (("m", 60, PER_MINUTE), ("h", 3600, PER_HOUR))


class McpRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = max(1, int(retry_after))


def _key(token_id: int, suffix: str) -> str:
    return f"mcp:rl:{token_id}:{suffix}"


def _hit(key: str, window: int) -> int:
    """Increment ``key`` inside a fixed ``window``; return the new count."""
    if cache.add(key, 0, window):
        cache.set(key + ":exp", time.time() + window, window)
    try:
        return int(cache.incr(key))
    except ValueError:
        # Expired between add() and incr(): restart the window.
        cache.set(key, 1, window)
        cache.set(key + ":exp", time.time() + window, window)
        return 1


def _retry_after(key: str, window: int) -> int:
    deadline = cache.get(key + ":exp")
    if not deadline:
        return window
    remaining = int(float(deadline) - time.time())
    return remaining if remaining > 0 else window


def check_budget(token_id: int) -> None:
    for suffix, window, limit in _WINDOWS:
        key = _key(token_id, suffix)
        try:
            count = _hit(key, window)
        except Exception as exc:  # noqa: BLE001 - any cache failure closes the gate
            logger.warning("MCP rate-limit cache unavailable (%s): %s", suffix, exc)
            raise McpRateLimited(window) from exc
        if count > limit:
            raise McpRateLimited(_retry_after(key, window))
```

- [ ] **Step 4: Run tests, commit**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp -q`
Expected: pass.

```bash
git add backend/apps/tenancy/mcp/ratelimit.py backend/tests/tenancy/mcp/test_ratelimit.py
git commit -m "feat(mcp): Per-token rate limit that fails closed

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Read tools — `list_jobs`, `get_job`, `list_applications`, `get_application`

**Files:**
- Create: `backend/apps/tenancy/mcp/tools_jobs.py`, `backend/apps/tenancy/mcp/tools_applications.py` (read half), `backend/apps/tenancy/mcp/serializers.py`
- Modify: `backend/tests/tenancy/mcp/test_rpc.py` (remove the skip)
- Test: `backend/tests/tenancy/mcp/test_tools_jobs.py`, `backend/tests/tenancy/mcp/test_tools_applications.py` (read tests)

**Interfaces:**
- Consumes: `register`, `validate`, `tool_ok`, `ToolError`, `ToolContext` (Task 3); `seed_platform`, `seed_listing` from `tests/jobs/test_api.py`.
- Produces: `serializers.job_summary(job, application) -> dict`, `serializers.job_detail(job, application) -> dict`, `serializers.application_out(app) -> dict`; helper `tools_applications.own_application(ctx, application_id) -> Application` (raises `application_not_found`) reused by Tasks 6–7.

Wire shapes (structuredContent):

```
job_summary: {id, platform: {slug, name, risk, apply_mechanism, auto_send}, title,
              company, url, location, salary, tags, scraped_at (ISO 8601),
              application_status (str|null), application_id (int|null)}
job_detail:  job_summary + {description, requirements, platform_specific_data}
application_out: {id, job_id, job_title, company, platform_slug, status, subject,
                  content, cover_letter, notes, documents: [{id, name, kind}],
                  sent_manually, sent_at, created_at, updated_at}
```

- [ ] **Step 1: Failing tests**

```python
# backend/tests/tenancy/mcp/test_tools_jobs.py
from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.jobs.models import Application, ApplicationStatus
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_api import seed_listing, seed_platform
from tests.support import make_user
from tests.tenancy.mcp.support import call_tool, make_token


def result_of(response):
    return response.json()["result"]


class ListJobsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.raw, _ = make_token(self.user, self.org)
        self.remoteok = seed_platform()
        self.linkedin = seed_platform("linkedin", name="LinkedIn", connector_type="browser_automation",
                                      apply_mechanism="in_platform_form", risk="high", auto_send=False)
        self.j1 = seed_listing(self.remoteok, title="Django Engineer")
        self.j2 = seed_listing(self.linkedin, title="Frontend Lead", company="Acme")

    def test_lists_with_platform_flags_and_null_application(self):
        r = result_of(call_tool(self.client, self.raw, "list_jobs"))
        self.assertFalse(r["isError"])
        jobs = r["structuredContent"]["jobs"]
        self.assertEqual({j["id"] for j in jobs}, {self.j1.id, self.j2.id})
        li = next(j for j in jobs if j["id"] == self.j2.id)
        self.assertEqual(li["platform"], {"slug": "linkedin", "name": "LinkedIn", "risk": "high",
                                          "apply_mechanism": "in_platform_form", "auto_send": False})
        self.assertIsNone(li["application_status"])

    def test_filters(self):
        sc = lambda **a: result_of(call_tool(self.client, self.raw, "list_jobs", a))["structuredContent"]
        self.assertEqual([j["id"] for j in sc(platform="linkedin")["jobs"]], [self.j2.id])
        self.assertEqual([j["id"] for j in sc(q="acme")["jobs"]], [self.j2.id])
        future = (timezone.now() + timedelta(days=1)).isoformat()
        self.assertEqual(sc(since=future)["jobs"], [])
        self.assertEqual(len(sc(limit=1)["jobs"]), 1)

    def test_limit_over_50_is_invalid_arguments(self):
        r = result_of(call_tool(self.client, self.raw, "list_jobs", {"limit": 51}))
        self.assertTrue(r["isError"])
        self.assertEqual(r["structuredContent"]["error"]["code"], "invalid_arguments")

    def test_overlay_is_own_application_only(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        Application.objects.create(org=other_org, user=other, job_listing=self.j1,
                                   status=ApplicationStatus.SENT)
        Application.objects.create(org=self.org, user=self.user, job_listing=self.j1,
                                   status=ApplicationStatus.REVIEW)
        jobs = result_of(call_tool(self.client, self.raw, "list_jobs"))["structuredContent"]["jobs"]
        mine = next(j for j in jobs if j["id"] == self.j1.id)
        self.assertEqual(mine["application_status"], "review")

    def test_token_is_pinned_to_its_org_not_the_users_other_orgs(self):
        # Same user, second workspace; the token belongs to org A only.
        from apps.tenancy.models import Membership, Organization, Role
        org_b = Organization.objects.create(name="B", slug="b-org")
        Membership.objects.create(org=org_b, user=self.user, role=Role.MEMBER)
        Application.objects.create(org=org_b, user=self.user, job_listing=self.j1,
                                   status=ApplicationStatus.SENT)
        jobs = result_of(call_tool(self.client, self.raw, "list_jobs"))["structuredContent"]["jobs"]
        self.assertIsNone(next(j for j in jobs if j["id"] == self.j1.id)["application_status"])

    def test_get_job_detail_and_not_found(self):
        r = result_of(call_tool(self.client, self.raw, "get_job", {"job_id": self.j1.id}))
        sc = r["structuredContent"]
        self.assertIn("description", sc)
        self.assertEqual(sc["platform_specific_data"], {"apply_email": "jobs@example.com"})
        r = result_of(call_tool(self.client, self.raw, "get_job", {"job_id": 999999}))
        self.assertEqual(r["structuredContent"]["error"]["code"], "job_not_found")
```

```python
# backend/tests/tenancy/mcp/test_tools_applications.py  (read half; Tasks 6–7 append)
from __future__ import annotations

from django.test import TestCase

from apps.jobs.models import Application, ApplicationStatus
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_api import seed_listing, seed_platform
from tests.support import make_user
from tests.tenancy.mcp.support import call_tool, make_token


def result_of(response):
    return response.json()["result"]


class ApplicationToolsBase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.raw, self.token = make_token(self.user, self.org)
        self.remoteok = seed_platform()
        self.linkedin = seed_platform("linkedin", name="LinkedIn", connector_type="browser_automation",
                                      apply_mechanism="in_platform_form", risk="high", auto_send=False)
        self.low_job = seed_listing(self.remoteok)
        self.high_job = seed_listing(self.linkedin, title="Lead")

    def call(self, name, **arguments):
        return result_of(call_tool(self.client, self.raw, name, arguments))

    def sc(self, name, **arguments):
        return self.call(name, **arguments)["structuredContent"]


class ReadApplicationTests(ApplicationToolsBase):
    def test_list_and_get_own_only(self):
        mine = Application.objects.create(org=self.org, user=self.user, job_listing=self.low_job,
                                          status=ApplicationStatus.REVIEW, subject="Hi")
        other = make_user()
        other_org = bootstrap_workspace(other).org
        theirs = Application.objects.create(org=other_org, user=other, job_listing=self.low_job)
        listed = self.sc("list_applications")["applications"]
        self.assertEqual([a["id"] for a in listed], [mine.id])
        self.assertEqual(self.sc("get_application", application_id=mine.id)["subject"], "Hi")
        self.assertEqual(self.sc("get_application", application_id=theirs.id)["error"]["code"],
                         "application_not_found")

    def test_list_filters_by_status(self):
        Application.objects.create(org=self.org, user=self.user, job_listing=self.low_job,
                                   status=ApplicationStatus.SENT)
        self.assertEqual(self.sc("list_applications", status="review")["applications"], [])
        self.assertEqual(len(self.sc("list_applications", status="sent")["applications"]), 1)
        self.assertEqual(self.sc("list_applications", status="bogus")["error"]["code"], "invalid_arguments")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_tools_jobs.py tests/tenancy/mcp/test_tools_applications.py -q`
Expected: every test fails with `-32602 Unknown tool` (KeyError on `structuredContent`).

- [ ] **Step 3: serializers.py**

```python
# backend/apps/tenancy/mcp/serializers.py
"""Plain-dict wire shapes for tool results (spec §5)."""
from __future__ import annotations

from apps.jobs.models import Application, JobListing


def _iso(dt):
    return dt.isoformat() if dt else None


def platform_out(platform) -> dict:
    return {"slug": platform.slug, "name": platform.name, "risk": platform.risk,
            "apply_mechanism": platform.apply_mechanism, "auto_send": platform.auto_send}


def job_summary(job: JobListing, application: Application | None) -> dict:
    return {
        "id": job.id, "platform": platform_out(job.platform), "title": job.title,
        "company": job.company, "url": job.url, "location": job.location,
        "salary": job.salary, "tags": job.tags or [], "scraped_at": _iso(job.scraped_at),
        "application_status": application.status if application else None,
        "application_id": application.id if application else None,
    }


def job_detail(job: JobListing, application: Application | None) -> dict:
    data = job_summary(job, application)
    data.update({"description": job.description, "requirements": job.requirements,
                 "platform_specific_data": job.platform_specific_data or {}})
    return data


def application_out(app: Application) -> dict:
    docs = [{"id": d.id, "name": d.name, "kind": d.kind}
            for d in app.documents.order_by("applicationdocument__position")]
    return {
        "id": app.id, "job_id": app.job_listing_id, "job_title": app.job_listing.title,
        "company": app.job_listing.company, "platform_slug": app.job_listing.platform_id,
        "status": app.status, "subject": app.subject, "content": app.generated_content,
        "cover_letter": app.generated_cover_letter, "notes": app.notes, "documents": docs,
        "sent_manually": app.sent_manually, "sent_at": _iso(app.sent_at),
        "created_at": _iso(app.created_at), "updated_at": _iso(app.updated_at),
    }
```

`app.documents` is an M2M through `ApplicationDocument`; the reverse ordering name is `applicationdocument__position`. If Django complains, order by `ApplicationDocument.objects.filter(application=app).select_related("document").order_by("position")` instead.

- [ ] **Step 4: tools_jobs.py**

```python
# backend/apps/tenancy/mcp/tools_jobs.py
"""list_jobs / get_job (spec §5). Listings are global; the overlay is the
token member's own application in the token's org, nothing else."""
from __future__ import annotations

from rest_framework import serializers

from apps.jobs.models import Application, JobListing
from apps.tenancy.mcp.serializers import job_detail, job_summary
from apps.tenancy.mcp.tools import ToolContext, ToolError, register, tool_ok, validate

MAX_LIMIT = 50


class ListJobsArgs(serializers.Serializer):
    platform = serializers.SlugField(required=False)
    q = serializers.CharField(required=False, max_length=100)
    since = serializers.DateTimeField(required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=MAX_LIMIT, default=20)


class GetJobArgs(serializers.Serializer):
    job_id = serializers.IntegerField(min_value=1)


def _overlay(ctx: ToolContext, job_ids):
    rows = Application.objects.for_org(ctx.org).filter(user=ctx.user, job_listing_id__in=list(job_ids))
    return {a.job_listing_id: a for a in rows}


@register("list_jobs", scope="jobs:read",
          description="List scraped job listings with this member's application status. "
                      "Filters: platform (slug), q (title/company), since (ISO datetime), limit (<=50).",
          input_schema={"type": "object", "properties": {
              "platform": {"type": "string"}, "q": {"type": "string"},
              "since": {"type": "string", "format": "date-time"},
              "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20}},
              "additionalProperties": False})
def list_jobs(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(ListJobsArgs, arguments)
    qs = JobListing.objects.select_related("platform").filter(platform__is_active=True)
    if args.get("platform"):
        qs = qs.filter(platform__slug=args["platform"])
    if args.get("q"):
        from django.db.models import Q
        qs = qs.filter(Q(title__icontains=args["q"]) | Q(company__icontains=args["q"]))
    if args.get("since"):
        qs = qs.filter(scraped_at__gte=args["since"])
    jobs = list(qs.order_by("-scraped_at", "-id")[: args["limit"]])
    overlay = _overlay(ctx, (j.id for j in jobs))
    data = {"jobs": [job_summary(j, overlay.get(j.id)) for j in jobs]}
    return tool_ok(data, f"{len(jobs)} job(s).")


@register("get_job", scope="jobs:read",
          description="One job listing with full description and requirements.",
          input_schema={"type": "object", "properties": {"job_id": {"type": "integer"}},
                        "required": ["job_id"], "additionalProperties": False})
def get_job(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(GetJobArgs, arguments)
    job = JobListing.objects.select_related("platform").filter(pk=args["job_id"]).first()
    if job is None:
        raise ToolError("job_not_found", "No such job listing.")
    overlay = _overlay(ctx, [job.id])
    return tool_ok(job_detail(job, overlay.get(job.id)), f"{job.title} at {job.company}")
```

- [ ] **Step 5: tools_applications.py (read half)**

```python
# backend/apps/tenancy/mcp/tools_applications.py
"""Application tools (spec §5). Every read starts from
``Application.objects.for_org(ctx.org).filter(user=ctx.user)``."""
from __future__ import annotations

from rest_framework import serializers

from apps.jobs.models import Application, ApplicationStatus
from apps.tenancy.mcp.serializers import application_out
from apps.tenancy.mcp.tools import ToolContext, ToolError, register, tool_ok, validate

_STATUSES = [c for c, _ in ApplicationStatus.choices]


def own_applications(ctx: ToolContext):
    return (Application.objects.for_org(ctx.org).filter(user=ctx.user)
            .select_related("job_listing", "job_listing__platform"))


def own_application(ctx: ToolContext, application_id: int) -> Application:
    app = own_applications(ctx).filter(pk=application_id).first()
    if app is None:
        raise ToolError("application_not_found", "No such application in this workspace.")
    return app


class ListApplicationsArgs(serializers.Serializer):
    status = serializers.ChoiceField(choices=_STATUSES, required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=50, default=20)


class ApplicationIdArgs(serializers.Serializer):
    application_id = serializers.IntegerField(min_value=1)


@register("list_applications", scope="applications:read",
          description="This member's applications in the workspace. Filter: status.",
          input_schema={"type": "object", "properties": {
              "status": {"type": "string", "enum": _STATUSES},
              "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
              "additionalProperties": False})
def list_applications(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(ListApplicationsArgs, arguments)
    qs = own_applications(ctx)
    if args.get("status"):
        qs = qs.filter(status=args["status"])
    apps = list(qs[: args["limit"]])
    return tool_ok({"applications": [application_out(a) for a in apps]}, f"{len(apps)} application(s).")


@register("get_application", scope="applications:read",
          description="One application with its draft content and attached documents.",
          input_schema={"type": "object", "properties": {"application_id": {"type": "integer"}},
                        "required": ["application_id"], "additionalProperties": False})
def get_application(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(ApplicationIdArgs, arguments)
    app = own_application(ctx, args["application_id"])
    return tool_ok(application_out(app), f"Application {app.id}: {app.status}")
```

- [ ] **Step 6: Un-skip the scope test in `test_rpc.py`, run, commit**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp -q && ../.venv/bin/pytest -q`
Expected: all pass.

```bash
git add backend/apps/tenancy/mcp backend/tests/tenancy/mcp
git commit -m "feat(mcp): Read tools for jobs and applications

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Write tools — `draft_application`, `update_application`

**Files:**
- Modify: `backend/apps/tenancy/mcp/tools_applications.py`
- Test: append to `backend/tests/tenancy/mcp/test_tools_applications.py`

**Interfaces:**
- Consumes: `own_application`, `ApplicationIdArgs` (Task 5); `Document`, `ApplicationDocument`; `AuditLog.objects.record`.
- Produces: tools `draft_application`, `update_application`. Error codes: `job_not_found`, `application_exists`, `document_not_found`, `application_locked`, `invalid_arguments`. Audit actions `mcp.draft_application`, `mcp.update_application`.

- [ ] **Step 1: Failing tests** (append)

```python
from django.core.files.base import ContentFile

from apps.jobs.models import Document
from apps.tenancy.models import AuditLog


def make_document(org, user, name="cv.pdf") -> Document:
    doc = Document(org=org, user=user, name=name, kind="cv")
    doc.file.save(name, ContentFile(b"%PDF-1.4"), save=False)
    doc.save()
    return doc


class DraftApplicationTests(ApplicationToolsBase):
    def test_creates_in_review_with_documents_and_audit(self):
        doc = make_document(self.org, self.user)
        sc = self.sc("draft_application", job_id=self.low_job.id, subject="Application",
                     content="Dear team", cover_letter="CL", notes="n", document_ids=[doc.id])
        self.assertEqual(sc["status"], "review")
        self.assertEqual(sc["documents"], [{"id": doc.id, "name": "cv.pdf", "kind": "cv"}])
        app = Application.objects.for_org(self.org).get()
        self.assertEqual(app.generated_content, "Dear team")
        actions = list(AuditLog.objects.for_org(self.org).values_list("action", flat=True))
        self.assertEqual(actions, ["mcp.draft_application"])
        payload = AuditLog.objects.for_org(self.org).get().payload
        self.assertEqual(payload["token_prefix"], self.token.prefix)

    def test_duplicate_is_application_exists(self):
        self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c")
        sc = self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c")
        self.assertEqual(sc["error"]["code"], "application_exists")
        self.assertEqual(sc["error"]["application_id"], Application.objects.for_org(self.org).get().id)

    def test_unknown_job(self):
        self.assertEqual(self.sc("draft_application", job_id=99999, subject="s", content="c")["error"]["code"],
                         "job_not_found")

    def test_foreign_document_is_document_not_found_and_nothing_created(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        foreign = make_document(other_org, other)
        sc = self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c",
                     document_ids=[foreign.id])
        self.assertEqual(sc["error"]["code"], "document_not_found")
        self.assertEqual(Application.all_objects.count(), 0)

    def test_missing_content_is_invalid_arguments(self):
        self.assertEqual(self.sc("draft_application", job_id=self.low_job.id)["error"]["code"],
                         "invalid_arguments")


class UpdateApplicationTests(ApplicationToolsBase):
    def _draft(self):
        return self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c")["id"]

    def test_partial_update_in_review(self):
        app_id = self._draft()
        sc = self.sc("update_application", application_id=app_id, content="new body")
        self.assertEqual(sc["content"], "new body")
        self.assertEqual(sc["subject"], "s")

    def test_replaces_document_set(self):
        app_id = self._draft()
        d1, d2 = make_document(self.org, self.user, "a.pdf"), make_document(self.org, self.user, "b.pdf")
        self.sc("update_application", application_id=app_id, document_ids=[d1.id, d2.id])
        sc = self.sc("update_application", application_id=app_id, document_ids=[d2.id])
        self.assertEqual([d["id"] for d in sc["documents"]], [d2.id])

    def test_locked_after_sent(self):
        app_id = self._draft()
        Application.all_objects.filter(pk=app_id).update(status=ApplicationStatus.SENT)
        self.assertEqual(self.sc("update_application", application_id=app_id, content="x")["error"]["code"],
                         "application_locked")

    def test_no_fields_is_invalid_arguments(self):
        app_id = self._draft()
        self.assertEqual(self.sc("update_application", application_id=app_id)["error"]["code"],
                         "invalid_arguments")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_tools_applications.py -q`
Expected: the new classes fail with `-32602 Unknown tool`.

- [ ] **Step 3: Implement** (append to `tools_applications.py`)

```python
from django.utils import timezone

from apps.jobs.models import ApplicationDocument, Document, JobListing
from apps.tenancy.models import AuditLog

EDITABLE = (ApplicationStatus.PENDING, ApplicationStatus.REVIEW)


def _audit(ctx: ToolContext, action: str, app: Application, **extra) -> None:
    AuditLog.objects.record(
        action=f"mcp.{action}", org=ctx.org, actor=ctx.user, target=app,
        payload={"token_prefix": ctx.token.prefix, **extra}, ip=ctx.ip,
    )


def _resolve_documents(ctx: ToolContext, ids: list[int]) -> list[Document]:
    if not ids:
        return []
    found = {d.id: d for d in Document.objects.for_org(ctx.org).filter(user=ctx.user, pk__in=ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ToolError("document_not_found", f"Unknown document id(s): {missing}.")
    return [found[i] for i in ids]


def _set_documents(app: Application, docs: list[Document]) -> None:
    ApplicationDocument.objects.filter(application=app).delete()
    ApplicationDocument.objects.bulk_create(
        [ApplicationDocument(application=app, document=d, position=i) for i, d in enumerate(docs)]
    )


class DraftArgs(serializers.Serializer):
    job_id = serializers.IntegerField(min_value=1)
    subject = serializers.CharField(max_length=255)
    content = serializers.CharField()
    cover_letter = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    document_ids = serializers.ListField(child=serializers.IntegerField(min_value=1),
                                         required=False, default=list)


class UpdateArgs(serializers.Serializer):
    application_id = serializers.IntegerField(min_value=1)
    subject = serializers.CharField(max_length=255, required=False)
    content = serializers.CharField(required=False)
    cover_letter = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    document_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)

    def validate(self, attrs):
        if set(attrs) == {"application_id"}:
            raise serializers.ValidationError({"fields": "Provide at least one field to change."})
        return attrs


_DRAFT_SCHEMA = {"type": "object", "properties": {
    "job_id": {"type": "integer"}, "subject": {"type": "string", "maxLength": 255},
    "content": {"type": "string", "description": "The application email / message body."},
    "cover_letter": {"type": "string"}, "notes": {"type": "string"},
    "document_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["job_id", "subject", "content"], "additionalProperties": False}


@register("draft_application", scope="applications:write",
          description="Create an application draft in REVIEW for a job. The assistant writes the "
                      "content; nothing is sent. Fails with application_exists if one exists.",
          input_schema=_DRAFT_SCHEMA)
def draft_application(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(DraftArgs, arguments)
    job = JobListing.objects.select_related("platform").filter(pk=args["job_id"]).first()
    if job is None:
        raise ToolError("job_not_found", "No such job listing.")
    existing = own_applications(ctx).filter(job_listing=job).first()
    if existing is not None:
        err = ToolError("application_exists", f"Application {existing.id} already exists for this job.")
        err.extra = {"application_id": existing.id}
        raise err
    docs = _resolve_documents(ctx, args["document_ids"])
    app = Application.objects.create(
        org=ctx.org, user=ctx.user, job_listing=job, status=ApplicationStatus.REVIEW,
        subject=args["subject"], generated_content=args["content"],
        generated_cover_letter=args["cover_letter"], notes=args["notes"],
    )
    _set_documents(app, docs)
    _audit(ctx, "draft_application", app, job_id=job.id)
    app = own_application(ctx, app.id)
    return tool_ok(application_out(app), f"Draft {app.id} created in REVIEW.")


_UPDATE_SCHEMA = {"type": "object", "properties": {
    "application_id": {"type": "integer"}, "subject": {"type": "string", "maxLength": 255},
    "content": {"type": "string"}, "cover_letter": {"type": "string"}, "notes": {"type": "string"},
    "document_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["application_id"], "additionalProperties": False}


@register("update_application", scope="applications:write",
          description="Edit a draft (PENDING/REVIEW only). document_ids replaces the whole set.",
          input_schema=_UPDATE_SCHEMA)
def update_application(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(UpdateArgs, arguments)
    app = own_application(ctx, args["application_id"])
    if app.status not in EDITABLE:
        raise ToolError("application_locked", f"Application is {app.status}; only PENDING/REVIEW drafts can be edited.")
    changed = []
    for arg, field in (("subject", "subject"), ("content", "generated_content"),
                       ("cover_letter", "generated_cover_letter"), ("notes", "notes")):
        if arg in args:
            setattr(app, field, args[arg])
            changed.append(field)
    if changed:
        app.save(update_fields=changed + ["updated_at"])
    if "document_ids" in args:
        _set_documents(app, _resolve_documents(ctx, args["document_ids"]))
        changed.append("documents")
    _audit(ctx, "update_application", app, fields=changed)
    app = own_application(ctx, app.id)
    return tool_ok(application_out(app), f"Application {app.id} updated.")
```

`ToolError.extra` must reach the envelope: in `tools.py` give `ToolError.__init__` an `extra: dict | None = None` parameter stored as `self.extra`, and make `tool_error(code, detail, **extra)` merge extras into `structuredContent["error"]`. Update `rpc._tools_call` to call `tool_error(exc.code, exc.detail, **(exc.extra or {}))`. Then replace the two `err.extra = …` lines above with `raise ToolError("application_exists", "...", extra={"application_id": existing.id})`.

- [ ] **Step 4: Run tests, suite, commit**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp -q && ../.venv/bin/pytest -q`
Expected: pass.

```bash
git add backend/apps/tenancy/mcp backend/tests/tenancy/mcp
git commit -m "feat(mcp): draft_application and update_application tools

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Send tools — `mark_sent_manually`, `send_application`

**Files:**
- Modify: `backend/apps/tenancy/mcp/tools_applications.py`
- Test: append to `backend/tests/tenancy/mcp/test_tools_applications.py`

**Interfaces:**
- Produces: tools `mark_sent_manually`, `send_application`. Error codes: `use_send_application`, `manual_only`, `mailbox_not_connected`, `not_implemented`, `application_locked`. Audit action `mcp.mark_sent_manually`.

- [ ] **Step 1: Failing tests** (append)

```python
from apps.tenancy.outbox.models import MailboxConnection, MailboxStatus


class SendToolsTests(ApplicationToolsBase):
    def _draft(self, job):
        return Application.objects.create(org=self.org, user=self.user, job_listing=job,
                                          status=ApplicationStatus.REVIEW, subject="s",
                                          generated_content="c")

    def test_mark_sent_manually_on_high_risk_platform(self):
        app = self._draft(self.high_job)
        sc = self.sc("mark_sent_manually", application_id=app.id)
        self.assertEqual(sc["status"], "sent")
        self.assertTrue(sc["sent_manually"])
        self.assertIsNotNone(sc["sent_at"])
        self.assertIn("mcp.mark_sent_manually",
                      list(AuditLog.objects.for_org(self.org).values_list("action", flat=True)))

    def test_mark_sent_manually_refused_on_auto_send_platform(self):
        app = self._draft(self.low_job)
        self.assertEqual(self.sc("mark_sent_manually", application_id=app.id)["error"]["code"],
                         "use_send_application")
        self.assertEqual(Application.all_objects.get(pk=app.id).status, "review")

    def test_mark_sent_manually_twice_is_locked(self):
        app = self._draft(self.high_job)
        self.sc("mark_sent_manually", application_id=app.id)
        self.assertEqual(self.sc("mark_sent_manually", application_id=app.id)["error"]["code"],
                         "application_locked")

    def test_send_application_refused_on_high_risk(self):
        app = self._draft(self.high_job)
        self.assertEqual(self.sc("send_application", application_id=app.id)["error"]["code"],
                         "manual_only")

    def test_send_application_needs_mailbox(self):
        app = self._draft(self.low_job)
        self.assertEqual(self.sc("send_application", application_id=app.id)["error"]["code"],
                         "mailbox_not_connected")

    def test_send_application_not_implemented_yet(self):
        MailboxConnection.objects.create(org=self.org, user=self.user, provider="smtp",
                                         email_address="me@example.de", credentials_encrypted=b"x",
                                         status=MailboxStatus.CONNECTED)
        app = self._draft(self.low_job)
        self.assertEqual(self.sc("send_application", application_id=app.id)["error"]["code"],
                         "not_implemented")
        self.assertEqual(Application.all_objects.get(pk=app.id).status, "review")

    def test_send_scope_required(self):
        raw, _ = make_token(self.user, self.org, scopes=["applications:read", "applications:write"])
        app = self._draft(self.high_job)
        r = result_of(call_tool(self.client, raw, "mark_sent_manually", {"application_id": app.id}))
        self.assertEqual(r["structuredContent"]["error"]["code"], "scope_missing")
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && ../.venv/bin/pytest tests/tenancy/mcp/test_tools_applications.py::SendToolsTests -q`
Expected: fail with unknown tool.

- [ ] **Step 3: Implement** (append)

```python
from apps.tenancy.outbox.models import MailboxConnection, MailboxStatus

SENDABLE = (ApplicationStatus.PENDING, ApplicationStatus.REVIEW)


def _platform_auto_sends(app: Application) -> bool:
    p = app.job_listing.platform
    return bool(p.auto_send and p.apply_mechanism == "email")


@register("mark_sent_manually", scope="applications:send",
          description="Record that the member applied by hand on a platform findajob never "
                      "automates (LinkedIn, Indeed, Wellfound). Refused on auto-send platforms.",
          input_schema={"type": "object", "properties": {"application_id": {"type": "integer"}},
                        "required": ["application_id"], "additionalProperties": False})
def mark_sent_manually(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(ApplicationIdArgs, arguments)
    app = own_application(ctx, args["application_id"])
    if app.status not in SENDABLE:
        raise ToolError("application_locked", f"Application is already {app.status}.")
    if _platform_auto_sends(app):
        raise ToolError("use_send_application",
                        "This platform sends by email from the member's mailbox; use send_application.")
    app.status = ApplicationStatus.SENT
    app.sent_manually = True
    app.sent_at = timezone.now()
    app.save(update_fields=["status", "sent_manually", "sent_at", "updated_at"])
    _audit(ctx, "mark_sent_manually", app)
    return tool_ok(application_out(app), f"Application {app.id} marked as sent manually.")


@register("send_application", scope="applications:send",
          description="Send the application email from the member's own connected mailbox. "
                      "Only on auto-send email platforms (RemoteOK, WeWorkRemotely, …).",
          input_schema={"type": "object", "properties": {"application_id": {"type": "integer"}},
                        "required": ["application_id"], "additionalProperties": False})
def send_application(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(ApplicationIdArgs, arguments)
    app = own_application(ctx, args["application_id"])
    if app.status not in SENDABLE:
        raise ToolError("application_locked", f"Application is already {app.status}.")
    if not _platform_auto_sends(app):
        raise ToolError("manual_only", "This platform is never automated; apply by hand, then call mark_sent_manually.")
    mailbox = (MailboxConnection.objects.for_org(ctx.org)
               .filter(user=ctx.user, status=MailboxStatus.CONNECTED).first())
    if mailbox is None:
        raise ToolError("mailbox_not_connected", "Connect a mailbox in Settings → Outbox first.")
    # Outbox spec phase 3 (applications/<id>/send/) is not built yet. The tool is
    # registered so clients can discover it; it dispatches nothing until then.
    raise ToolError("not_implemented", "Sending from the mailbox is not available yet.")
```

- [ ] **Step 4: Run tests, suite, commit**

Run: `cd backend && ../.venv/bin/pytest -q`
Expected: pass.

```bash
git add backend/apps/tenancy/mcp backend/tests/tenancy/mcp
git commit -m "feat(mcp): mark_sent_manually and send_application guarded by the risk table

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: `verify_platform` module + nginx route + deploy note

**Files:**
- Create: `backend/apps/common/verify/mcp.py`
- Modify: `backend/apps/common/verify/__init__.py` (register `"mcp"` **before** `"logout"`)
- Modify: `deploy/nginx.conf` (new `location ^~ /mcp/` block, copy of the `/api/` block)
- Modify: `.claude/skills/findajob-deploy/SKILL.md` (one paragraph: `/mcp/` route + `MCP_SERVER_ENABLED`)
- Test: `backend/tests/common/test_verify_mcp.py`

**Interfaces:**
- Consumes: `VerifyContext` (`ctx.session`, `ctx.env`, `ctx.base_url` — check `base.py` for the exact attribute holding the site origin; `ctx.api("/x/")` builds `/api/v1/x/`, which is wrong here, so build `f"{origin}/mcp/"` directly).
- Produces: `run(ctx) -> ModuleReport("mcp", …)`.

- [ ] **Step 1: Failing test**

```python
# backend/tests/common/test_verify_mcp.py
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from apps.common.verify import MODULES
from apps.common.verify import mcp


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _ctx(env=None, responses=()):
    session = mock.Mock()
    session.post = mock.Mock(side_effect=list(responses))
    ctx = mock.Mock()
    ctx.session = session
    ctx.env = env or {}
    ctx.origin = "https://findajob.pro"
    return ctx


class VerifyMcpTests(SimpleTestCase):
    def test_registered_before_logout(self):
        names = list(MODULES)
        self.assertIn("mcp", names)
        self.assertLess(names.index("mcp"), names.index("logout"))

    def test_unauthenticated_probe_expects_401_envelope(self):
        ctx = _ctx(responses=[_Resp(401, {"error": {"code": "token_missing", "detail": "x"}})])
        report = mcp.run(ctx)
        self.assertFalse(report.failed)
        self.assertEqual(report.results[0].status, "PASS")

    def test_endpoint_absent_is_skip(self):
        ctx = _ctx(responses=[_Resp(404, {"error": {"code": "not_found", "detail": "x"}})])
        report = mcp.run(ctx)
        self.assertEqual(report.results[0].status, "SKIP")

    def test_with_token_asserts_tool_names(self):
        ctx = _ctx(env={"MCP_VERIFY_TOKEN": "fjp_" + "a" * 40}, responses=[
            _Resp(401, {"error": {"code": "token_missing", "detail": "x"}}),
            _Resp(200, {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": n} for n in mcp.EXPECTED_TOOLS]}}),
        ])
        report = mcp.run(ctx)
        self.assertFalse(report.failed)
```

Adjust `_ctx` to whatever `VerifyContext` exposes for the origin (read `apps/common/verify/base.py` first) and adjust `report.results[0].status` to the real field names on `CheckResult`.

- [ ] **Step 2: Implement**

```python
# backend/apps/common/verify/mcp.py
"""mcp module — the /mcp/ endpoint answers with the envelope, and (with
MCP_VERIFY_TOKEN) advertises the phase-1 tool set. Never FAILs for a missing
credential or an undeployed route."""
from __future__ import annotations

from apps.common.verify.base import TIMEOUT, ModuleReport, VerifyContext, failed, passed, skipped

EXPECTED_TOOLS = {
    "list_jobs", "get_job", "list_applications", "get_application",
    "draft_application", "update_application", "mark_sent_manually", "send_application",
}
_PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _url(ctx: VerifyContext) -> str:
    return f"{ctx.origin}/mcp/"   # see Interfaces: replace `origin` with the real attribute


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    resp = ctx.session.post(_url(ctx), json=_PING, timeout=TIMEOUT)
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    code = ((payload or {}).get("error") or {}).get("code")
    if resp.status_code == 404:
        results.append(skipped("mcp endpoint", "not deployed on this target (MCP_SERVER_ENABLED off or no nginx route)"))
        return ModuleReport("mcp", results)
    if resp.status_code == 401 and code == "token_missing":
        results.append(passed("mcp endpoint rejects anonymous with envelope"))
    else:
        results.append(failed("mcp endpoint", f"expected 401 token_missing, got HTTP {resp.status_code} {code}"))
        return ModuleReport("mcp", results)

    token = ctx.env.get("MCP_VERIFY_TOKEN")
    if not token:
        results.append(skipped("tools/list", "set MCP_VERIFY_TOKEN to probe with a real token"))
        return ModuleReport("mcp", results)
    resp = ctx.session.post(_url(ctx), json=_LIST, timeout=TIMEOUT,
                            headers={"Authorization": f"Bearer {token}"})
    try:
        names = {t["name"] for t in resp.json()["result"]["tools"]}
    except (ValueError, KeyError, TypeError):
        results.append(failed("tools/list", f"HTTP {resp.status_code}: {resp.text[:120]}"))
        return ModuleReport("mcp", results)
    missing = EXPECTED_TOOLS - names
    results.append(failed("tools/list", f"missing tools: {sorted(missing)}") if missing
                   else passed("tools/list advertises the phase-1 tool set"))
    return ModuleReport("mcp", results)
```

In `apps/common/verify/__init__.py` add `mcp` to the import tuple and insert `MODULES["mcp"] = mcp.run` immediately **before** the `logout` line.

- [ ] **Step 3: nginx**

In `deploy/nginx.conf`, directly after the closing brace of `location ^~ /api/ { … }`, add a copy of that block with these differences: `location ^~ /mcp/`, `client_max_body_size 1m;`, and a leading comment:

```nginx
    # MCP endpoint (docs/superpowers/specs/2026-09-26-mcp-server-design.md).
    # Same upstream as /api/. Bearer-only and JSON-RPC, so no cookie or CSRF
    # concerns; the 256 KB body cap is enforced by Django, 1m here is slack.
```

Do not touch the CSP: `/mcp/` is never loaded by a browser page.

- [ ] **Step 4: Deploy skill note**

Append to `.claude/skills/findajob-deploy/SKILL.md` under the nginx section:

> **`/mcp/`** — proxied to the api container exactly like `/api/`. The Django side is off until `MCP_SERVER_ENABLED=true` is in the api container's env; `verify_platform --module mcp` SKIPs while either half is missing, and PASSes the anonymous probe once both are live. `MCP_VERIFY_TOKEN` (a token created in Settings → Connected assistants) enables the `tools/list` check.

- [ ] **Step 5: Run suite, commit**

Run: `cd backend && ../.venv/bin/pytest -q`
Expected: pass.

```bash
git add backend/apps/common/verify backend/tests/common/test_verify_mcp.py deploy/nginx.conf .claude/skills/findajob-deploy/SKILL.md
git commit -m "feat(mcp): verify_platform module, nginx route and deploy note

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Settings → "Connected assistants" in `web/app.html` + `web/api.js`

**REQUIRED SUB-SKILLS before touching UI:** `ui-ux-pro-max` (global rule) and `voltagent-design` (project tokens). Read both, then edit.

**Files:**
- Modify: `web/api.js` (ENDPOINTS + three client functions + demo fixture)
- Modify: `web/app.html` (Settings screen: new card)

**Interfaces:**
- Consumes: `GET/POST /api/v1/mcp-tokens/`, `DELETE /api/v1/mcp-tokens/<id>/` (Task 2).
- Produces: `listMcpTokens()`, `createMcpToken({name, scopes, expires_in_days})`, `revokeMcpToken(id)` in `web/api.js`; in demo mode they resolve from a fixture array `MCP_TOKENS` and never hit the network.

- [ ] **Step 1: api.js**

Add to `ENDPOINTS`:

```js
  mcpTokens: '/mcp-tokens/',                // GET list, POST {name, scopes, expires_in_days} -> {token once}, DELETE /mcp-tokens/:id/
```

Add next to the other account-data functions, following the exact pattern the file uses for demo-vs-live (look at how `getMe`/`platforms` branch on the demo flag and copy it):

```js
export const MCP_SCOPES = ['jobs:read', 'applications:read', 'applications:write', 'applications:send'];

const MCP_TOKENS = [
  { id: 1, name: 'Claude Desktop', prefix: 'fjp_demo1234', scopes: MCP_SCOPES.slice(0, 3),
    expires_at: null, last_used_at: null, status: 'active', created_at: '2026-09-01T09:00:00Z' },
];

export const listMcpTokens = () => request(ENDPOINTS.mcpTokens);
export const createMcpToken = (body) => request(ENDPOINTS.mcpTokens, { method: 'POST', body });
export const revokeMcpToken = (id) => request(`${ENDPOINTS.mcpTokens}${id}/`, { method: 'DELETE' });
```

Wire `MCP_TOKENS` into whatever fixture map the demo path reads (the file has one; register `mcpTokens: MCP_TOKENS` there).

- [ ] **Step 2: app.html card**

Inside the Settings screen's template, after the Integrations card, add a card titled **Connected assistants** with:
- a table: Name · Prefix · Scopes · Expires · Last used · Status · Revoke button;
- a "New token" form: name input, four scope checkboxes (`applications:send` unchecked by default), expiry select (30 / 90 / 365 days);
- on create success, a one-time reveal block showing the plaintext token with a copy button and this snippet, substituting the token:

```
claude mcp add findajob --transport http https://findajob.pro/mcp/ --header "Authorization: Bearer <token>"
```

- an inline note: "Shown once. Store it in your assistant now." Use the existing card, table, button and input chrome from the page's other Settings cards; no new colours or fonts. Responsive rules go in the page's `<style>` block with `!important` like the neighbouring rules.

- [ ] **Step 3: Manual check**

Run from `web/`: `python3 -m http.server 8080`, open `http://localhost:8080/app.html?demo=1`, go to Settings, confirm the card renders with the fixture token, create/revoke work against the fixture, and the page has no console errors. Then with the backend running (`manage.py runserver`, `MCP_SERVER_ENABLED=true` in `backend/.env`) log in and create a real token; call:

```bash
curl -s -X POST http://127.0.0.1:8000/mcp/ -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | python3 -m json.tool
```

Expected: eight tools listed.

- [ ] **Step 4: Commit**

```bash
git add web/api.js web/app.html
git commit -m "feat(web): Connected assistants card for MCP tokens in Settings

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: Finish

- [ ] Run the whole suite one last time: `cd backend && ../.venv/bin/pytest -q`.
- [ ] Update the spec header: `Status: **Phase 1 implemented** (PAT auth, 8 tools; send_application returns not_implemented until outbox phase 3).`
- [ ] Add one paragraph to `CLAUDE.md` under Architecture: "MCP server at `/mcp/` (`apps/tenancy/mcp/`), bearer-only, feature-flagged by `MCP_SERVER_ENABLED`; tokens at `/api/v1/mcp-tokens/`. Spec in `docs/superpowers/specs/2026-09-26-mcp-server-design.md`."
- [ ] Commit: `docs(mcp): Mark phase 1 implemented`.
- [ ] Invoke `superpowers:finishing-a-development-branch`.
