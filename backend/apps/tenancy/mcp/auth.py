"""Bearer authentication for ``/mcp/`` (spec §4).

No session fallback, ever: a browser cookie must not drive a CSRF-exempt
endpoint. Lookup is by prefix, then a constant-time hash compare.
"""
from __future__ import annotations

import hmac

from django.utils import timezone

from apps.legal.request_meta import client_ip as _proxy_aware_client_ip
from apps.tenancy.mcp.models import PREFIX_LENGTH, TOKEN_LENGTH, TOKEN_PREFIX, McpToken, hash_token
from apps.tenancy.models import Membership

#: Update ``last_used_at`` at most this often per token (spec §4).
TOUCH_INTERVAL_SECONDS = 60


class McpAuthError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def client_ip(request) -> str | None:
    """The caller's address, honouring ``TRUSTED_PROXY_COUNT``.

    The single place ``/mcp/`` derives an IP; behind Traefik -> nginx,
    ``REMOTE_ADDR`` is the proxy for every request.
    """
    return _proxy_aware_client_ip(request)


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
        token.last_used_ip = client_ip(request)
        token.save(update_fields=["last_used_at", "last_used_ip"])
    return token, membership
