"""The login flow itself. All of the security lives here.

Two public entry points, both called from :mod:`apps.accounts.views`:

* :func:`request_login_code` — spec §3's ``POST /api/v1/auth/request-code/``
* :func:`verify_login_code`  — spec §3's ``POST /api/v1/auth/verify-code/``

Invariants this module exists to hold
-------------------------------------

**The plaintext code lives in exactly one process, for one request.** It is
generated, hashed, written as a hash, interpolated into an email, and dropped.
It is never returned, never logged, never handed to a queue and never written to
an audit payload (spec §4). The one exception is
``manage.py issue_login_code``, whose entire purpose is to print it (spec §6).

**``request-code`` is branch-free** (spec §6). Nothing in that path so much as
queries :class:`~apps.accounts.models.User`. Not "queries it and ignores the
result" — does not query it. That is the only way to guarantee identical status,
identical body *and* identical timing for an address with an account and one
without, and it is why :class:`~apps.accounts.models.EmailOTP` has no FK to
``User``.

**Up to three codes stay live at once, and a resend never supersedes** (spec
§4). German providers greylist, deliver both codes seconds apart, and the user
types the older one. Verify therefore iterates every live row newest-first with
:func:`~django.utils.crypto.constant_time_compare`. The cap is enforced by
retiring the *oldest* surplus row when a fourth is issued, so the invariant is
"at most three live", never "only the newest works".

**The credential is locked, not the identity** (spec §5). There is no per-email
account lockout anywhere in this file: an attacker making 30 wrong guesses must
never deny login to the mailbox owner. What dies is the code — five wrong
guesses exhaust every live row for that address and force the attacker back to
the inbox — plus a per-(email, /24) verify budget in
:mod:`apps.accounts.ratelimit`.

**Module boundary** (spec §2). Nothing here imports ``Organization``,
``Membership`` or ``AuditLog``. The single permitted call into
:mod:`apps.tenancy` is :func:`~apps.tenancy.services.bootstrap_workspace`.
Consequence worth stating: accounts writes **no** ``AuditLog`` rows; security
events go to the ``findajob.accounts`` logger, which is filtered by
``apps.common.logging.RedactingFilter``.
"""

from __future__ import annotations

import inspect
import ipaddress
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import login as auth_login
from django.db import transaction
from django.utils import timezone

from apps.accounts import ratelimit
from apps.accounts.mailer import send_login_code
from apps.accounts.models import (
    MAX_LIVE_OTPS,
    MAX_OTP_ATTEMPTS,
    OTP_CODE_LENGTH,
    OTP_RESEND_COOLDOWN_SECONDS,
    OTP_TTL_SECONDS,
    PURPOSE_LOGIN,
    EmailOTP,
    User,
    hash_code,
    normalize_email,
)
from apps.common.errors import ApiError, ErrorCode, RateLimited

logger = logging.getLogger("findajob.accounts")

#: Django ships exactly one authentication backend by default and we add none.
#: Naming it explicitly is still right: ``login()`` guesses only while there is
#: exactly one, so the day a second backend appears this keeps working instead
#: of raising ``ValueError`` inside a successful login.
AUTH_BACKEND = "django.contrib.auth.backends.ModelBackend"

#: How far back :func:`_classify_failure` looks for a dead row matching the
#: submitted code, so it can answer ``code_expired`` instead of ``invalid_code``.
DEAD_CODE_LOOKBACK = timedelta(seconds=OTP_TTL_SECONDS * 3)

#: Bound on that lookup, so a flooded address cannot turn one verify into a
#: hundred HMACs.
DEAD_CODE_SCAN_LIMIT = 10


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def _strip_port(value: str) -> str:
    """``"1.2.3.4:5678"`` -> ``"1.2.3.4"``; ``"[::1]:443"`` -> ``"::1"``."""
    value = value.strip()
    if value.startswith("["):
        closing = value.find("]")
        if closing != -1:
            return value[1:closing]
        return value
    if value.count(":") == 1:  # exactly one colon => IPv4:port, not IPv6
        return value.split(":", 1)[0]
    return value


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    candidate = _strip_port(str(value))
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def client_ip(request) -> str | None:
    """The address the *user* is at, not the address of the proxy in front.

    In production Traefik terminates TLS and forwards to gunicorn, so
    ``REMOTE_ADDR`` is the proxy and every visitor on earth shares it. Left
    alone, the 20/hour per-IP budget in spec §5 would throttle the entire site
    to 20 login codes an hour — a self-inflicted outage.

    ``X-Forwarded-For`` cannot simply be trusted either: a client can send its
    own, and then it picks its own rate-limit bucket. The resolution is
    ``settings.TRUSTED_PROXY_COUNT``: with *N* trusted proxies the client is the
    (N+1)-th entry from the right, because every proxy appends the address it
    received the connection from. A spoofed value is pushed left, past the
    index we read, and is ignored. With the default of 0 this returns
    ``REMOTE_ADDR`` and nothing else.
    """
    meta = getattr(request, "META", None) or {}
    remote = _valid_ip(meta.get("REMOTE_ADDR"))

    try:
        trusted = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0) or 0)
    except (TypeError, ValueError):  # pragma: no cover - misconfiguration
        trusted = 0

    if trusted > 0:
        chain = [
            part.strip()
            for part in str(meta.get("HTTP_X_FORWARDED_FOR", "")).split(",")
            if part.strip()
        ]
        if chain:
            index = max(0, min(len(chain) - trusted, len(chain) - 1))
            forwarded = _valid_ip(chain[index])
            if forwarded:
                return forwarded
    return remote


def user_agent_of(request) -> str:
    meta = getattr(request, "META", None) or {}
    return str(meta.get("HTTP_USER_AGENT", ""))[:400]


def mask_email(email: str) -> str:
    """``"george@example.de"`` -> ``"g•••@example.de"``.

    The domain stays intact — the user just typed it, so hiding it only makes
    the confirmation useless — while the local part is reduced to its first
    character and a fixed-width mask that carries no length information.
    """
    address = normalize_email(email)
    local, _, domain = address.partition("@")
    if not domain:
        return "•••"
    head = local[:1] or "•"
    return f"{head}•••@{domain}"


# ---------------------------------------------------------------------------
# Issuing
# ---------------------------------------------------------------------------


def generate_code() -> str:
    """A cryptographically random zero-padded code.

    ``secrets.randbelow`` is uniform over ``[0, 10**6)``; ``random`` would be
    seeded predictably enough to matter here.
    """
    return f"{secrets.randbelow(10 ** OTP_CODE_LENGTH):0{OTP_CODE_LENGTH}d}"


def _retire_surplus_codes(email: str, purpose: str, now) -> int:
    """Keep the live set at ``MAX_LIVE_OTPS`` once the new row is added.

    Retires by moving ``expires_at`` to now rather than by setting
    ``consumed_at``: ``consumed_at`` means "this code logged somebody in", and
    an audit trail that cannot tell those apart is worthless.
    """
    surplus = list(
        EmailOTP.objects.live(email=email, purpose=purpose, now=now)
        .newest_first()
        .values_list("pk", flat=True)[MAX_LIVE_OTPS - 1 :]
    )
    if not surplus:
        return 0
    return EmailOTP.objects.filter(pk__in=surplus).update(expires_at=now)


def issue_code(
    *, email: str, ip: str | None = None, purpose: str = PURPOSE_LOGIN, now=None
) -> tuple[EmailOTP, str]:
    """Write one code row and return it together with the plaintext.

    The plaintext is returned, never stored. Callers must hand it straight to
    :func:`apps.accounts.mailer.send_login_code` (or, for the break-glass
    management command, to stdout) and let it fall out of scope.

    Bypasses every rate limit on purpose — the budgets belong to the HTTP
    endpoint, not to the primitive, so ``manage.py issue_login_code`` still
    works when the cache is down and the limits are saturated.
    """
    email = normalize_email(email)
    now = now or timezone.now()
    code = generate_code()
    with transaction.atomic():
        _retire_surplus_codes(email, purpose, now)
        otp = EmailOTP.objects.create(
            email=email,
            code_hash=hash_code(code, email=email),
            purpose=purpose,
            expires_at=now + timedelta(seconds=OTP_TTL_SECONDS),
            request_ip=_valid_ip(ip),
        )
    return otp, code


def cooldown_active_in_db(email: str, now=None) -> int:
    """Database fallback for the 60 s resend cooldown (spec §5).

    Used only when the cache is unreachable. The row it reads is one we write
    on every request anyway, so this costs one indexed query and needs no new
    state. Returns the number of seconds left, or 0 when a send is allowed.
    """
    now = now or timezone.now()
    window_start = now - timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS)
    latest = (
        EmailOTP.objects.filter(
            email=normalize_email(email),
            purpose=PURPOSE_LOGIN,
            created_at__gt=window_start,
        )
        .newest_first()
        .first()
    )
    if latest is None:
        return 0
    elapsed = (now - latest.created_at).total_seconds()
    return max(1, int(OTP_RESEND_COOLDOWN_SECONDS - elapsed))


# ---------------------------------------------------------------------------
# POST /api/v1/auth/request-code/
# ---------------------------------------------------------------------------


def request_login_code(*, request, email: str, lang: str) -> dict:
    """Issue and deliver a login code. Always the same shape (spec §6).

    :returns: ``{"masked_email": …, "expires_in": 600, "resend_in": 60}``
    :raises apps.common.errors.RateLimited: 429, with a truthful ``retry_after``
    :raises apps.common.errors.DeliveryUnavailable: 503, when the provider could
        not be reached — never a silent green 202
    """
    address = normalize_email(email)
    ip = client_ip(request)

    # 1. Resend cooldown. `claim_resend_cooldown` is atomic, so two tabs racing
    #    each other cannot both send.
    cooldown = ratelimit.claim_resend_cooldown(address)
    if not cooldown.allowed:
        raise RateLimited(cooldown.retry_after)
    if cooldown.degraded:
        # Cache is down: fail OPEN, but not blind. This is the DB fallback spec
        # §5 prescribes.
        remaining = cooldown_active_in_db(address)
        if remaining:
            raise RateLimited(remaining)

    # 2. The three counted budgets. Fail OPEN.
    budgets = ratelimit.check_request_budgets(address, ip)
    if not budgets.allowed:
        logger.info("Login code request rejected by the %s budget.", budgets.budget)
        raise RateLimited(budgets.retry_after)

    # 3. Write the row, then deliver. This order matters: a code that exists in
    #    the database but was never delivered is merely unusable, whereas a code
    #    that was delivered but never written cannot be verified at all.
    otp, code = issue_code(email=address, ip=ip)

    try:
        send_login_code(
            email=address, code=code, lang=lang, expires_in=OTP_TTL_SECONDS
        )
    except Exception:
        # Delivery failed and we are about to answer 503. Undo both side
        # effects, or the user is told "try again" and then hits a 60 s
        # cooldown they were charged for an email that never arrived.
        EmailOTP.objects.filter(pk=otp.pk).delete()
        ratelimit.release_resend_cooldown(address)
        raise
    finally:
        del code

    return {
        "masked_email": mask_email(address),
        "expires_in": OTP_TTL_SECONDS,
        "resend_in": OTP_RESEND_COOLDOWN_SECONDS,
    }


# ---------------------------------------------------------------------------
# POST /api/v1/auth/verify-code/
# ---------------------------------------------------------------------------


def _classify_failure(email: str, code: str, now) -> ApiError:
    """Decide between ``invalid_code``, ``code_expired`` and ``too_many_attempts``.

    Only a *dead row that the submitted code actually matches* produces
    ``code_expired``. Deriving it from "an expired row exists for this address"
    would answer a question the caller has not proved they may ask; matching
    first means the answer is only ever given to whoever already holds that
    code, which is the mailbox owner.
    """
    dead = (
        EmailOTP.objects.filter(
            email=email,
            purpose=PURPOSE_LOGIN,
            created_at__gte=now - DEAD_CODE_LOOKBACK,
        )
        .newest_first()[:DEAD_CODE_SCAN_LIMIT]
    )
    for row in dead:
        if row.is_live(now):
            continue
        if row.matches(code):
            if row.is_exhausted():
                return ApiError(
                    ErrorCode.TOO_MANY_ATTEMPTS,
                    "Too many wrong attempts for that code. Request a new one.",
                    429,
                )
            return ApiError(
                ErrorCode.CODE_EXPIRED,
                "That code has expired. Request a new one.",
                400,
            )
    return ApiError(ErrorCode.INVALID_CODE, "That code is not correct.", 400)


def _record_consent(user, *, ip, user_agent, ui_language, client_terms_version):
    """Hand registration consent to ``apps.legal`` (spec §10).

    Imported lazily and defensively because ``apps.legal`` is written by a
    parallel work stream: a half-built legal app must not make login impossible.
    When the function is absent this logs loudly and returns ``None`` — the user
    still gets in, and the missing consent row is visible in the logs rather
    than hidden behind a 500.

    Anything the recorder *raises* propagates untouched, on purpose: that is how
    ``409 terms_outdated`` reaches the client, and because the whole verify runs
    in one transaction, a raise also rolls back the code burn so the user can
    resubmit with the current version.
    """
    try:
        from apps.legal import services as legal_services  # noqa: PLC0415
    except ImportError:  # pragma: no cover - only while apps.legal is unbuilt
        logger.error("apps.legal.services is missing; consent was NOT recorded.")
        return None

    recorder = getattr(legal_services, "record_registration_consent", None)
    if recorder is None:
        logger.error(
            "apps.legal.services.record_registration_consent is missing; "
            "consent was NOT recorded."
        )
        return None

    kwargs = {
        "ip": ip,
        "user_agent": user_agent,
        "ui_language": ui_language,
        "client_terms_version": client_terms_version,
    }
    # Bind before calling rather than try/except TypeError around the call: a
    # TypeError raised *inside* the recorder would otherwise be misread as a
    # signature mismatch and the recorder invoked a second time.
    try:
        bound = inspect.signature(recorder).bind(user, **kwargs)
    except TypeError:
        bound = inspect.signature(recorder).bind(
            user, ip, user_agent, ui_language, client_terms_version
        )
    return recorder(*bound.args, **bound.kwargs)


@transaction.atomic
def verify_login_code(
    *,
    request,
    email: str,
    code: str,
    accepts_terms: bool,
    lang: str,
    terms_version: str | None = None,
) -> tuple[User, bool]:
    """Verify a code, log the session in, and return ``(user, created)``.

    Everything below happens in one transaction, so any failure — a missing
    consent, stale terms, a deactivated account — rolls the code burn back and
    leaves the user able to try again.
    """
    address = normalize_email(email)
    ip = client_ip(request)
    now = timezone.now()

    # 1. Per-(email, /24) guessing budget. Fails CLOSED (spec §5): an unmetered
    #    verify endpoint is an online oracle against a 10^6 keyspace.
    budget = ratelimit.check_verify_budget(address, ip)
    if not budget.allowed:
        raise RateLimited(budget.retry_after)

    # 2. Lock and read every live row, newest first.
    #    `of=("self",)` and not a bare `select_for_update()`: the bare form
    #    against a nullable join is rejected outright by Postgres, and being
    #    explicit about locking only the driving table is the actual intent.
    live_rows = list(
        EmailOTP.objects.live(email=address, now=now)
        .newest_first()
        .select_for_update(of=("self",))
    )

    # 3. Compare against all of them without short-circuiting, so the time taken
    #    depends on how many codes are live and on nothing else.
    matched: EmailOTP | None = None
    for row in live_rows:
        is_match = row.matches(code)
        if is_match and matched is None:
            matched = row

    if matched is None:
        _burn_attempts(live_rows)
        ratelimit.record_verify_failure(address, ip)
        if live_rows and all(row.attempts >= MAX_OTP_ATTEMPTS for row in live_rows):
            logger.info("All live login codes exhausted for one address.")
            raise ApiError(
                ErrorCode.TOO_MANY_ATTEMPTS,
                "Too many wrong attempts for that code. Request a new one.",
                429,
            )
        raise _classify_failure(address, code, now)

    # 4. The code is right. From here on the caller has proved mailbox control,
    #    so it is safe to talk about the account.
    existing = User.objects.filter(email=address).first()
    created = existing is None

    if created and not accepts_terms:
        # Spec §10: an affirmative checkbox, not passive grey text. Raised
        # *before* the burn — the transaction rolls back, so the user can tick
        # the box and resubmit the same code instead of losing it.
        raise ApiError(
            ErrorCode.TERMS_REQUIRED,
            "You must accept the Terms and the Privacy Policy to create an account.",
            400,
        )

    # 5. Single-use burn.
    matched.consumed_at = now
    matched.save(update_fields=["consumed_at"])

    if created:
        user = User.objects.create_user(email=address, lang=lang, is_verified=True)
        # Spec §10: consent is written inside the same transaction that creates
        # the user, and `apps.legal` resolves the binding document server-side.
        _record_consent(
            user,
            ip=ip,
            user_agent=user_agent_of(request),
            ui_language=lang,
            client_terms_version=terms_version,
        )
    else:
        user = existing
        updates = []
        if not user.is_verified:
            user.is_verified = True
            updates.append("is_verified")
        if lang and user.lang != lang:
            user.lang = lang
            updates.append("lang")
        if updates:
            user.save(update_fields=[*updates, "updated_at"])

    if not user.is_active:
        # Deliberately after the burn, and deliberately a generic 403: the code
        # was spent, and the response says nothing about why.
        logger.warning("Login blocked for a deactivated account.")
        raise ApiError(
            ErrorCode.PERMISSION_DENIED, "This account cannot sign in.", 403
        )

    # 6. Their own workspace. Idempotent, and the only call this app is allowed
    #    to make into apps.tenancy (spec §2).
    from apps.tenancy.services import bootstrap_workspace  # noqa: PLC0415

    bootstrap_workspace(user)

    # 7. Session. `login()` cycles the session key and rotates the CSRF token,
    #    which is what makes session fixation across the anonymous -> logged-in
    #    boundary a non-issue.
    auth_login(request, user, backend=AUTH_BACKEND)

    ratelimit.reset_verify_failures(address, ip)
    logger.info("Login succeeded (new account: %s).", created)
    return user, created


def _burn_attempts(rows: list[EmailOTP]) -> None:
    """Charge one wrong guess to every code that was compared.

    All of them, not just the newest. The credential being guessed against is
    "the set of codes currently live for this address" — that is exactly the set
    :func:`verify_login_code` compares against — so five wrong guesses must
    retire the whole set. Charging only the newest row would leave the older
    ones guessable forever.

    The value is clamped in Python rather than incremented with ``F()``: the
    ``otp_attempts_bounded`` check constraint refuses anything above 5, and the
    rows are already locked with ``select_for_update``, so there is no race to
    lose and no way to write a 6.
    """
    for row in rows:
        row.attempts = min(row.attempts + 1, MAX_OTP_ATTEMPTS)
        row.save(update_fields=["attempts"])
