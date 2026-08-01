"""OTP rate limiting (implementation spec §5).

The budgets
-----------

=====================  ===========  ==========================================
Budget                 Limit        Key namespace
=====================  ===========  ==========================================
Resend cooldown        60 s         ``otp:cd:{email}``
Per email              5 / hour     ``otp:req:{email}``
Per IP                 20 / hour    ``otp:ip:{ip}``
Global                 500 / hour   ``otp:global``
Verify failures        10 / 15 min  ``otp:vf:{email}:{subnet}``  (email, /24)
=====================  ===========  ==========================================

Three things about this module are deliberate and should not be "simplified".

**1. No** ``cache.ttl()``. It does not exist in Django's cache API — it is a
``django-redis`` extension, and ``django-redis`` is not on the approved
dependency list. The deadline is therefore stored explicitly in a sibling key
and the remaining wait is computed from it, exactly as spec §5 prescribes. A
client that gets a 429 always gets a truthful, non-zero ``retry_after``; it can
never raise ``AttributeError``.

**2. Redis down degrades, not 500.** Every single cache call goes through
:func:`_safe`, which swallows the exception and reports ``degraded``. What the
caller does with that differs by endpoint, and the difference is the whole
point (spec §5):

* ``request-code`` fails **OPEN** — the caller falls back to the database for
  the 60 s cooldown (a row it writes anyway). A login outage is worse than a
  temporarily loosened limit.
* ``verify-code`` fails **CLOSED**, but only for the subnet budget: an
  unmetered verify endpoint is an online guessing oracle.

**3. Identifiers are hashed into the key, not embedded raw.** Two reasons. A
raw address makes the cache key up to 254 characters, past the 250-character
limit Django warns about; and it would leave Redis holding a plaintext list of
every address that ever attempted a login, which is personal data sitting in a
store with no retention policy and no encryption. The digest is keyed with
``SECRET_KEY`` so it is not reversible by rainbow table. Budget semantics are
completely unchanged — the same address still maps to the same key.

**Lock the credential, not the identity** (spec §5). Nothing here can lock an
account: there is no per-email verify lockout, because 30 wrong guesses from an
attacker must never deny login to the mailbox owner. The real control is the
per-row ``attempts <= 5`` burn in :mod:`apps.accounts.services`, which forces an
attacker back to the inbox for a fresh code.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import Callable

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("findajob.accounts.ratelimit")

# ---------------------------------------------------------------------------
# Budgets — spec §5
# ---------------------------------------------------------------------------

RESEND_COOLDOWN_SECONDS = 60

PER_EMAIL_LIMIT = 5
PER_EMAIL_WINDOW = 3600

PER_IP_LIMIT = 20
PER_IP_WINDOW = 3600

GLOBAL_LIMIT = 500
GLOBAL_WINDOW = 3600

VERIFY_FAILURE_LIMIT = 10
VERIFY_FAILURE_WINDOW = 15 * 60

#: Suffix of the sibling key that stores the window deadline as a unix
#: timestamp. See the module docstring, point 1.
_DEADLINE_SUFFIX = ":exp"


@dataclass(frozen=True)
class Decision:
    """The outcome of a budget check.

    ``retry_after`` is only meaningful when ``allowed`` is False, and is always
    at least 1 — a client that sleeps for 0 s hammers straight back.
    ``degraded`` means at least one cache call raised, so the caller may need a
    fallback (``request-code`` falls back to the database for the cooldown).
    ``budget`` names the budget that rejected, for logging only; it is never
    returned to the client, because telling an attacker *which* limit they hit
    tells them how the limits are shaped.
    """

    allowed: bool
    retry_after: int = 0
    degraded: bool = False
    budget: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - convenience only
        return self.allowed


ALLOWED = Decision(allowed=True)


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------


def _digest(value: str) -> str:
    """Stable, non-reversible, fixed-length identifier for a cache key."""
    secret = str(getattr(settings, "SECRET_KEY", "")).encode("utf-8", "replace")
    return hashlib.sha256(secret + b"|" + value.encode("utf-8", "replace")).hexdigest()[:32]


def subnet_of(ip: str | None) -> str:
    """The /24 (IPv4) or /64 (IPv6) network an address belongs to.

    Spec §5 pins the verify-failure budget to ``(email, /24 subnet)``. IPv6 has
    no /24 worth speaking of — a single customer is routinely handed a /64, and
    the VPS itself egresses over IPv6 (spec §6.1) — so /64 is the equivalent
    granularity there.
    """
    if not ip:
        return "unknown"
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return "unknown"
    prefix = 24 if address.version == 4 else 64
    network = ipaddress.ip_network(f"{address}/{prefix}", strict=False)
    return str(network)


def cooldown_key(email: str) -> str:
    return f"otp:cd:{_digest(email)}"


def per_email_key(email: str) -> str:
    return f"otp:req:{_digest(email)}"


def per_ip_key(ip: str | None) -> str:
    return f"otp:ip:{_digest(ip or 'unknown')}"


def global_key() -> str:
    return "otp:global"


def verify_failure_key(email: str, ip: str | None) -> str:
    return f"otp:vf:{_digest(email)}:{_digest(subnet_of(ip))}"


# ---------------------------------------------------------------------------
# Cache primitives — every one of them survives a dead Redis
# ---------------------------------------------------------------------------

_FAILED = object()


def _safe(operation: Callable[[], object], *, what: str):
    """Run a cache call, returning ``_FAILED`` instead of raising.

    Catching bare ``Exception`` is intentional. The redis client raises its own
    exception hierarchy (``ConnectionError``, ``TimeoutError``,
    ``ResponseError``, …) which Django does not wrap, and none of it is worth
    enumerating here: *any* cache failure means "the budget is unknown", and
    the endpoint's fail-open/fail-closed policy decides what that costs.
    """
    try:
        return operation()
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.warning("Rate-limit cache unavailable during %s: %s", what, exc)
        return _FAILED


def _deadline_key(key: str) -> str:
    return key + _DEADLINE_SUFFIX


def _set_deadline(key: str, window: int) -> None:
    _safe(
        lambda: cache.set(_deadline_key(key), time.time() + window, window),
        what=f"deadline({key})",
    )


def _remaining(key: str, window: int) -> int:
    """Seconds until the window rolls over. Never 0, never negative."""
    raw = _safe(lambda: cache.get(_deadline_key(key)), what=f"remaining({key})")
    if raw is _FAILED or raw in (None, 0):
        # The deadline key was evicted, never written, or the cache is down.
        # Quoting the whole window is the safe answer: too long is a slightly
        # annoyed user, too short is a client that retries into another 429.
        return window
    try:
        remaining = int(float(raw) - time.time())
    except (TypeError, ValueError):  # pragma: no cover - corrupt cache value
        return window
    return remaining if remaining > 0 else window


def _consume(key: str, limit: int, window: int, *, budget: str) -> Decision:
    """Count one hit against a fixed window budget.

    Returns an allowed decision while the counter is at or below ``limit``, and
    a 429-worthy one after. ``degraded`` is set when the cache could not be
    reached, in which case the decision is **allowed** — the caller decides
    whether that is acceptable.
    """
    created = _safe(lambda: cache.add(key, 0, window), what=f"add({key})")
    if created is _FAILED:
        return Decision(allowed=True, degraded=True, budget=budget)
    if created:
        # Only on creation: re-setting the deadline on every hit would slide the
        # window forward forever and the reported retry_after would be a lie.
        _set_deadline(key, window)

    count = _safe(lambda: cache.incr(key), what=f"incr({key})")
    if count is _FAILED:
        # Two distinct causes, same treatment. Either the backend is down, or
        # the key expired between add() and incr() and Django's cache API
        # signals that with ValueError. Restart the window rather than guess.
        restarted = _safe(lambda: cache.set(key, 1, window), what=f"reset({key})")
        if restarted is _FAILED:
            return Decision(allowed=True, degraded=True, budget=budget)
        _set_deadline(key, window)
        count = 1

    try:
        count = int(count)
    except (TypeError, ValueError):  # pragma: no cover - corrupt cache value
        return Decision(allowed=True, degraded=True, budget=budget)

    if count <= limit:
        return ALLOWED
    return Decision(
        allowed=False, retry_after=max(1, _remaining(key, window)), budget=budget
    )


def _peek(key: str, limit: int, window: int, *, budget: str,
          fail_closed: bool) -> Decision:
    """Read a counter without incrementing it."""
    value = _safe(lambda: cache.get(key, 0), what=f"get({key})")
    if value is _FAILED:
        if fail_closed:
            return Decision(
                allowed=False, retry_after=window, degraded=True, budget=budget
            )
        return Decision(allowed=True, degraded=True, budget=budget)
    try:
        count = int(value or 0)
    except (TypeError, ValueError):  # pragma: no cover - corrupt cache value
        count = 0
    if count < limit:
        return ALLOWED
    return Decision(
        allowed=False, retry_after=max(1, _remaining(key, window)), budget=budget
    )


# ---------------------------------------------------------------------------
# request-code — fails OPEN
# ---------------------------------------------------------------------------


def claim_resend_cooldown(email: str) -> Decision:
    """Claim the 60 s per-address send slot.

    ``cache.add`` is atomic (``SET NX`` on Redis), so two concurrent requests
    for the same address cannot both win. When the cache is unreachable this
    returns ``allowed=True, degraded=True`` and the caller **must** fall back to
    the database check in :func:`apps.accounts.services.cooldown_active_in_db`.
    """
    key = cooldown_key(email)
    claimed = _safe(
        lambda: cache.add(key, 1, RESEND_COOLDOWN_SECONDS), what=f"add({key})"
    )
    if claimed is _FAILED:
        return Decision(allowed=True, degraded=True, budget="cooldown")
    if claimed:
        _set_deadline(key, RESEND_COOLDOWN_SECONDS)
        return ALLOWED
    return Decision(
        allowed=False,
        retry_after=max(1, _remaining(key, RESEND_COOLDOWN_SECONDS)),
        budget="cooldown",
    )


def release_resend_cooldown(email: str) -> None:
    """Give the slot back.

    Called when delivery failed and the endpoint is answering 503: the user
    never received a code, so making them wait 60 s to try again would be
    punishing them for our outage.
    """
    key = cooldown_key(email)
    _safe(lambda: cache.delete(key), what=f"delete({key})")
    _safe(lambda: cache.delete(_deadline_key(key)), what=f"delete({key}:exp)")


def check_request_budgets(email: str, ip: str | None) -> Decision:
    """The three counted budgets behind ``POST /auth/request-code/``.

    Order matters only for which budget name gets logged; all three are
    consumed for an allowed request. Fails OPEN throughout (spec §5).
    """
    decisions = [
        _consume(per_email_key(email), PER_EMAIL_LIMIT, PER_EMAIL_WINDOW,
                 budget="per_email"),
        _consume(per_ip_key(ip), PER_IP_LIMIT, PER_IP_WINDOW, budget="per_ip"),
        _consume(global_key(), GLOBAL_LIMIT, GLOBAL_WINDOW, budget="global"),
    ]
    degraded = any(decision.degraded for decision in decisions)
    rejected = [decision for decision in decisions if not decision.allowed]
    if rejected:
        worst = max(rejected, key=lambda decision: decision.retry_after)
        return Decision(
            allowed=False,
            retry_after=worst.retry_after,
            degraded=degraded,
            budget=worst.budget,
        )
    return Decision(allowed=True, degraded=degraded)


# ---------------------------------------------------------------------------
# verify-code — fails CLOSED
# ---------------------------------------------------------------------------


def check_verify_budget(email: str, ip: str | None) -> Decision:
    """Is this ``(email, /24)`` pair still allowed to guess?

    Read-only: a *successful* verify must not consume budget, or a user who
    logs in ten times in fifteen minutes would lock themselves out.
    :func:`record_verify_failure` does the counting.

    Fails CLOSED (spec §5): with no cache there is no bound on online guessing
    against a 6-digit space, and this is the only budget in the module for
    which that trade goes the other way.
    """
    return _peek(
        verify_failure_key(email, ip),
        VERIFY_FAILURE_LIMIT,
        VERIFY_FAILURE_WINDOW,
        budget="verify_failures",
        fail_closed=True,
    )


def record_verify_failure(email: str, ip: str | None) -> Decision:
    """Count one wrong guess. Returns the decision for the *next* attempt."""
    return _consume(
        verify_failure_key(email, ip),
        VERIFY_FAILURE_LIMIT,
        VERIFY_FAILURE_WINDOW,
        budget="verify_failures",
    )


def reset_verify_failures(email: str, ip: str | None) -> None:
    """Clear the guessing budget after a successful login.

    Deliberately scoped to the (email, subnet) pair that just proved it holds
    the mailbox, so a legitimate user who fumbled a few codes starts clean.
    """
    key = verify_failure_key(email, ip)
    _safe(lambda: cache.delete(key), what=f"delete({key})")
    _safe(lambda: cache.delete(_deadline_key(key)), what=f"delete({key}:exp)")
