"""Where a consent record's ``ip`` and ``user_agent`` come from.

A consent record is evidence, so the address on it should be the visitor's, not
a reverse proxy's. In production the chain is::

    visitor -> Traefik -> nginx -> gunicorn

which means ``REMOTE_ADDR`` is nginx's container address for every request on
earth. The visitor's address is only in ``X-Forwarded-For``.

Why the *left-most* entry is the wrong answer
---------------------------------------------

``deploy/nginx.conf`` proxies with ``$proxy_add_x_forwarded_for``, which
**appends** to whatever the client sent. Traefik does the same. So what Django
receives is::

    X-Forwarded-For: <anything the visitor invented>, <real visitor>, <traefik>
                      ^ attacker-controlled              ^ appended by Traefik
                                                                    ^ by nginx

Reading ``xff[0]`` — the usual snippet — reads the attacker's string. The only
entries that can be trusted are the ones *appended by our own proxies*, counted
from the right. With ``N`` trusted proxies in front of Django, the visitor is
``xff[-N]``: each trusted hop appends the address of the peer it actually spoke
to, and the visitor is the peer of the outermost one.

Configuration
-------------

``TRUSTED_PROXY_COUNT`` (env, default ``0``):

* ``0`` — no proxy is trusted; ``REMOTE_ADDR`` is used and ``X-Forwarded-For``
  is ignored entirely. Correct for local development and for any deployment
  where Django is reached directly.
* ``2`` — the production topology above (Traefik + nginx).

Setting it too high hands an attacker control of the recorded address; setting
it too low records a proxy address. Too low is merely useless, too high is a
forgery vector, so the default is the useless one.

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

#: Never walk an unbounded header. A forged ``X-Forwarded-For`` with 10 000
#: entries is a cheap way to burn CPU in the validator.
MAX_FORWARDED_ENTRIES = 32

__all__ = ["client_ip", "client_user_agent", "trusted_proxy_count"]


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


def trusted_proxy_count() -> int:
    try:
        return max(0, int(getattr(settings, "TRUSTED_PROXY_COUNT", 0) or 0))
    except (TypeError, ValueError):
        return 0


def client_ip(request) -> str | None:
    """The visitor's IP address, or ``None`` when it cannot be established."""
    meta = getattr(request, "META", None) or {}
    remote_addr = _valid_ip(meta.get("REMOTE_ADDR", ""))

    proxies = trusted_proxy_count()
    if not proxies:
        return remote_addr

    raw = meta.get("HTTP_X_FORWARDED_FOR", "") or ""
    entries = [part.strip() for part in raw.split(",") if part.strip()]
    if len(entries) > MAX_FORWARDED_ENTRIES:
        entries = entries[-MAX_FORWARDED_ENTRIES:]
    if len(entries) < proxies:
        # The request did not traverse the full trusted chain (a health check
        # from inside the network, a misconfiguration, or someone talking to
        # gunicorn directly). Trust nothing in the header.
        return remote_addr
    return _valid_ip(entries[-proxies]) or remote_addr


def client_user_agent(request) -> str:
    """The ``User-Agent`` header, truncated and stripped of NUL bytes.

    Django's model fields have no NUL validator (``ProhibitNullCharacters`` is
    form-only), and a ``\\x00`` in a header would blow up the PostgreSQL insert.
    """
    meta = getattr(request, "META", None) or {}
    value = meta.get("HTTP_USER_AGENT", "") or ""
    return value.replace("\x00", "").strip()[:MAX_USER_AGENT_LENGTH]
