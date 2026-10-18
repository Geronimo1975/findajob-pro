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
