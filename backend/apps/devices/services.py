"""Minting, reading and revoking trusted devices.

See ``docs/2026-08-22-trusted-device-spec.md``.

Everything that touches the cookie lives here, so there is exactly one
definition of how a device cookie is written and read. The views in
``apps.accounts`` call these by name — the same lazy cross-app service call the
login path already makes into ``apps.tenancy``.

**The plaintext secret lives in one process, for one request.** It is generated,
hashed, written as a hash, set as a cookie, and dropped. It is never returned in
a body, never logged, never put in an audit payload.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.devices.models import (
    COOKIE_VALUE_SEPARATOR,
    DEVICE_SECRET_BYTES,
    MAX_DEVICE_USER_AGENT_LENGTH,
    TRUSTED_DEVICE_TTL_DAYS,
    TrustedDevice,
    hash_secret,
)

logger = logging.getLogger("findajob.devices")

#: Spec §3. Deliberately not prefixed with ``__Host-``: that prefix forbids a
#: ``Domain`` attribute and requires ``Secure``, which would make the cookie
#: silently vanish in local development over http.
DEVICE_COOKIE_NAME = "findajob_device"


def cookie_max_age() -> int:
    return TRUSTED_DEVICE_TTL_DAYS * 24 * 60 * 60


def _cookie_is_secure() -> bool:
    """Mirror the session cookie, so dev over http still works (spec §3)."""
    return bool(getattr(settings, "SESSION_COOKIE_SECURE", False))


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


def trust_device(*, user, ip=None, user_agent: str = "", now=None) -> tuple[TrustedDevice, str]:
    """Create one device row and return it with the plaintext secret.

    The secret is returned, never stored. The only correct thing to do with it
    is hand it straight to :func:`set_device_cookie` and let it fall out of
    scope.
    """
    now = now or timezone.now()
    secret = secrets.token_urlsafe(DEVICE_SECRET_BYTES)
    device = TrustedDevice(
        user=user,
        expires_at=now + timedelta(days=TRUSTED_DEVICE_TTL_DAYS),
        ip=ip,
        user_agent=(user_agent or "")[:MAX_DEVICE_USER_AGENT_LENGTH],
    )
    # `device_id` has a callable default, so it is populated on instantiation —
    # which it must be, because the hash is keyed by it.
    device.secret_hash = hash_secret(secret, device_id=device.device_id)
    device.save()
    return device, secret


def build_cookie_value(device: TrustedDevice, secret: str) -> str:
    return f"{device.device_id}{COOKIE_VALUE_SEPARATOR}{secret}"


def set_device_cookie(response, device: TrustedDevice, secret: str) -> None:
    """Write the device cookie onto ``response``."""
    response.set_cookie(
        DEVICE_COOKIE_NAME,
        build_cookie_value(device, secret),
        max_age=cookie_max_age(),
        httponly=True,
        samesite="Lax",
        secure=_cookie_is_secure(),
        path="/",
    )


def clear_device_cookie(response) -> None:
    """Delete the device cookie — used when it turned out to be unusable."""
    response.delete_cookie(
        DEVICE_COOKIE_NAME, path="/", samesite="Lax"
    )


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _split_cookie(raw: str | None) -> tuple[str, str] | None:
    if not raw or COOKIE_VALUE_SEPARATOR not in raw:
        return None
    device_id, _, secret = raw.partition(COOKIE_VALUE_SEPARATOR)
    if not device_id or not secret:
        return None
    return device_id, secret


def resolve_device(request, *, user=None, now=None) -> TrustedDevice | None:
    """The live :class:`TrustedDevice` this request carries, or ``None``.

    Returns ``None`` for every failure mode without distinguishing them — no
    cookie, malformed cookie, unknown id, wrong secret, expired, revoked, wrong
    owner. Spec §4: the caller's only correct response to any of them is the
    same ``401``, so a finer taxonomy would leak without buying anything.
    """
    parts = _split_cookie(request.COOKIES.get(DEVICE_COOKIE_NAME))
    if parts is None:
        return None
    device_id, secret = parts

    now = now or timezone.now()
    try:
        device = (
            TrustedDevice.objects.select_related("user")
            .filter(device_id=device_id)
            .first()
        )
    except (ValueError, TypeError, ValidationError):
        # A cookie whose id half is not a UUID at all. `UUIDField.to_python`
        # raises `ValidationError`; older paths raise `ValueError`. Either way
        # it is just an untrusted cookie, not a server error.
        return None
    if device is None or not device.is_live(now):
        return None
    if not device.matches(secret):
        return None
    if user is not None and device.user_id != getattr(user, "pk", None):
        return None
    if not getattr(device.user, "is_active", False):
        return None
    return device


def mark_device_used(device: TrustedDevice, *, ip=None, user_agent: str = "", now=None) -> None:
    """Record that a device just minted a session."""
    device.last_used_at = now or timezone.now()
    fields = ["last_used_at"]
    if ip:
        device.ip = ip
        fields.append("ip")
    if user_agent:
        device.user_agent = user_agent[:MAX_DEVICE_USER_AGENT_LENGTH]
        fields.append("user_agent")
    device.save(update_fields=fields)


def revoke_device(device: TrustedDevice, *, now=None) -> None:
    """Retire one device. Idempotent."""
    if device.revoked_at is not None:
        return
    device.revoked_at = now or timezone.now()
    device.save(update_fields=["revoked_at"])
