"""Rate limiting for the Django admin login.

Django ships none. The security review's finding was precise: Traefik basicAuth
sits in front of ``/admin/``, so an *internet* attacker never reaches the Django
login form — but "in front of" is not "instead of". Anything already inside the
``gethub_gethub-network`` docker network reaches nginx directly and bypasses
Traefik entirely, and that network is shared with a dozen unrelated stacks on
the same box. An unthrottled admin login reachable from every neighbouring
container is a real, if narrow, brute-force surface, and the admin is the one
account that can read every tenant's CVs and consent records.

This closes it with the budget machinery :mod:`apps.accounts.ratelimit` already
provides, so there is one implementation of "count hits in a window", one set of
degradation semantics, and one place where the cache-failure policy lives.

Design notes:

* Keyed on ``(submitted username, client subnet)``. Keying on the username alone
  would let anyone lock the founder out of his own admin by failing logins for
  his address — the same identity-vs-credential mistake the OTP design corrected
  (spec §5, "lock the credential, not the identity"). Keying on IP alone lets a
  botnet spread the guesses.
* Wired to ``user_login_failed``, which Django sends for the admin form, the
  auth backend and DRF alike — so this cannot be sidestepped by finding another
  login view later.
* **Fails open** when the cache is unreachable. A Redis outage must not lock the
  operator out of the admin during the very incident he is trying to inspect;
  the basicAuth gate is still standing in that scenario.
"""

from __future__ import annotations

import logging

from django.contrib.auth.signals import user_login_failed
from django.core.exceptions import PermissionDenied
from django.dispatch import receiver

from apps.accounts.ratelimit import Decision, _consume, _digest, _peek, subnet_of

logger = logging.getLogger("findajob.admin")

#: Failed admin logins allowed per (username, subnet) before the pair is frozen.
ADMIN_LOGIN_MAX_FAILURES = 8

#: Window and freeze duration, in seconds.
ADMIN_LOGIN_WINDOW = 15 * 60

_BUDGET = "admin_login"


def admin_login_key(username: str, ip: str | None) -> str:
    return f"adm:lf:{_digest((username or '').strip().lower())}:{_digest(subnet_of(ip))}"


def check_admin_login(username: str, ip: str | None) -> Decision:
    """Peek at the budget without spending it. Called before authenticating."""
    return _peek(
        admin_login_key(username, ip),
        ADMIN_LOGIN_MAX_FAILURES,
        ADMIN_LOGIN_WINDOW,
        budget=_BUDGET,
        # Fail OPEN: a Redis outage must not lock the operator out of the admin
        # during the very incident he opened it to investigate. The Traefik
        # basicAuth gate is still standing in that scenario.
        fail_closed=False,
    )


def record_admin_login_failure(username: str, ip: str | None) -> Decision:
    return _consume(
        admin_login_key(username, ip),
        ADMIN_LOGIN_MAX_FAILURES,
        ADMIN_LOGIN_WINDOW,
        budget=_BUDGET,
    )


def _client_ip(request) -> str | None:
    if request is None:
        return None
    # apps.legal.request_meta owns the trusted-proxy walk; import lazily so this
    # module stays importable in contexts where legal is not installed.
    try:
        from apps.legal.request_meta import client_ip

        return client_ip(request)
    except Exception:  # pragma: no cover - defensive only
        return request.META.get("REMOTE_ADDR")


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    username = (credentials or {}).get("username") or (credentials or {}).get("email") or ""
    decision = record_admin_login_failure(username, _client_ip(request))
    if not decision.allowed:
        logger.warning(
            "admin login budget exhausted (subnet=%s)", subnet_of(_client_ip(request))
        )


def enforce_admin_login_budget(request, username: str) -> None:
    """Raise :class:`PermissionDenied` when the pair is frozen.

    ``PermissionDenied`` rather than a 429 body: the admin login is a plain HTML
    form, and Django renders 403 for it. The response deliberately does not say
    which budget was hit or how long is left — that shapes the attacker's next
    attempt and this endpoint has no legitimate automated client to inform.
    """
    decision = check_admin_login(username, _client_ip(request))
    if not decision.allowed:
        raise PermissionDenied("Too many failed sign-in attempts.")
