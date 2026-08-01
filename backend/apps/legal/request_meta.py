"""Where a consent record's ``ip`` and ``user_agent`` come from.

A consent record is evidence, so the address on it should be the visitor's, not
the reverse proxy's. In production the chain is
``visitor -> Traefik -> nginx -> gunicorn``, which means ``REMOTE_ADDR`` is a
private container address unless we deliberately read ``X-Forwarded-For``.

``X-Forwarded-For`` is client-supplied and trivially spoofable, so reading it is
opt-in: set ``TRUST_X_FORWARDED_FOR=True`` (env) **only** when every request
really does arrive through a proxy that rewrites the header. Behind a trusted
proxy the left-most entry is the originating client; without one, a visitor can
put whatever they like there, which is why the default is off and the fallback
is ``REMOTE_ADDR``.

Anything that does not validate as an IPv4/IPv6 literal is dropped rather than
stored: ``GenericIPAddressField`` is a real ``inet`` column on PostgreSQL and a
junk value raises ``DataError`` at insert time — a 500 on the consent write.
"""

from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_ipv46_address

#: ``ConsentRecord.user_agent`` is a CharField; keep the two in step.
MAX_USER_AGENT_LENGTH = 400

__all__ = ["client_ip", "client_user_agent"]


def _valid_ip(value: str) -> str | None:
    candidate = (value or "").strip()
    if not candidate:
        return None
    # "[2001:db8::1]:443" and "203.0.113.4:1234" both show up in the wild.
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    try:
        validate_ipv46_address(candidate)
    except ValidationError:
        return None
    return candidate


def client_ip(request) -> str | None:
    """The visitor's IP address, or ``None`` when it cannot be established."""
    meta = getattr(request, "META", None) or {}
    if getattr(settings, "TRUST_X_FORWARDED_FOR", False):
        forwarded = meta.get("HTTP_X_FORWARDED_FOR", "")
        for part in forwarded.split(","):
            address = _valid_ip(part)
            if address is not None:
                return address
    return _valid_ip(meta.get("REMOTE_ADDR", ""))


def client_user_agent(request) -> str:
    """The ``User-Agent`` header, truncated and stripped of NUL bytes.

    Django's model fields have no NUL validator (``ProhibitNullCharacters`` is
    form-only), and a ``\\x00`` in a header would blow up the PostgreSQL insert.
    """
    meta = getattr(request, "META", None) or {}
    value = meta.get("HTTP_USER_AGENT", "") or ""
    return value.replace("\x00", "").strip()[:MAX_USER_AGENT_LENGTH]
