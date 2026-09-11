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

from django.contrib.auth import login as auth_login
from django.db import transaction
from django.utils import timezone
from django.utils.translation import get_language

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
from apps.legal import request_meta as legal_request_meta

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


def _valid_ip(value: str | None) -> str | None:
    """Sanitise an address supplied by something other than a request.

    Only used for the ``ip`` argument of :func:`issue_code`, which the
    break-glass management command passes as ``None``. Request-borne addresses
    go through :func:`client_ip` instead.
    """
    if not value:
        return None
    candidate = str(value).strip()
    if candidate.startswith("[") and "]" in candidate:
        candidate = candidate[1 : candidate.index("]")]
    elif candidate.count(":") == 1:  # exactly one colon => IPv4:port
        candidate = candidate.split(":", 1)[0]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


# The client's real address matters twice over here: it selects the 20/hour
# per-IP budget and the /24 verify-failure budget (spec §5). Getting it wrong in
# either direction is expensive — read `apps.legal.request_meta` for the full
# argument. In short: behind Traefik every visitor shares one REMOTE_ADDR, so
# leaving the budgets keyed on it would throttle the entire site to twenty login
# codes an hour; and `X-Forwarded-For[0]` is attacker-controlled, so trusting it
# would let a client pick its own bucket.
#
# `apps.legal.request_meta` is declared in `config.settings.base` as *the* one
# implementation ("anything that records or rate-limits by IP has to use it"),
# so this imports it rather than carrying a second copy that could drift. It is
# a pure module — django.conf and two validators, no models — so importing it
# from a sibling app is free of ordering hazards.
client_ip = legal_request_meta.client_ip
user_agent_of = legal_request_meta.client_user_agent


def effective_lang(requested: str | None) -> str:
    """The language to write an email in, or to stamp on a brand-new account.

    ``web/api.js``'s ``verifyCode(email, code, accepts_terms)`` sends no ``lang``
    — that is the pinned contract (spec §3) and not something to change here —
    so without a fallback every account created through the real frontend would
    be stamped ``en``, including a German user who just read a German code.
    ``LocaleMiddleware`` has already resolved the request's language from the
    ``findajob_lang`` cookie and ``Accept-Language`` by the time a view runs, so
    that is the honest default.

    Only used as a *default*. An existing user's stored preference is updated
    solely when the client sends ``lang`` explicitly — otherwise logging in from
    a borrowed English browser would silently rewrite it.
    """
    from apps.accounts.serializers import normalise_lang  # noqa: PLC0415

    return normalise_lang(requested) if requested else normalise_lang(get_language())


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
            email=address,
            code=code,
            lang=effective_lang(lang),
            expires_in=OTP_TTL_SECONDS,
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


def _match_or_charge(address: str, code: str, now) -> tuple[int | None, bool]:
    """Find the live row matching ``code``; charge a failed attempt if none does.

    Returns ``(matched_pk_or_None, all_live_rows_now_exhausted)``.

    This is its own transaction, and that boundary is the point. A failed
    attempt has to be **durable**: if the increment shared a transaction with
    the rejection, raising the ``invalid_code`` error would roll the increment
    straight back and ``attempts`` would sit at 0 forever — the cap would exist
    in the schema and do nothing, which is the whole defence against online
    guessing. So the counter is committed here and the exception is raised by
    the caller, after this block has closed.
    """
    with transaction.atomic():
        # `of=("self",)` rather than a bare `select_for_update()`: the bare form
        # next to a nullable join is rejected outright by Postgres, and being
        # explicit about locking only the driving table is the actual intent.
        live_rows = list(
            EmailOTP.objects.live(email=address, now=now)
            .newest_first()
            .select_for_update(of=("self",))
        )

        # Compare against all of them without short-circuiting, so the time
        # taken depends on how many codes are live and on nothing else.
        matched: EmailOTP | None = None
        for row in live_rows:
            is_match = row.matches(code)
            if is_match and matched is None:
                matched = row

        if matched is not None:
            return matched.pk, False

        _burn_attempts(live_rows)
        exhausted = bool(live_rows) and all(
            row.attempts >= MAX_OTP_ATTEMPTS for row in live_rows
        )
        return None, exhausted


def _record_login_failure(request, email: str, ip: str | None, reason: str) -> None:
    """Audit a failed verify attempt. No org, no actor: neither is known yet."""
    from apps.tenancy.services import record_security_event  # noqa: PLC0415

    record_security_event(
        action="auth.login_failed",
        ip=ip,
        user_agent=user_agent_of(request),
        target_type="email",
        target_id=email,
        payload={"reason": reason},
    )


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

    Two transactions, deliberately:

    1. :func:`_match_or_charge` — locks the live rows, compares, and on failure
       **commits** the attempt increment before anything raises.
    2. The success path below — burn, create, consent, workspace, session — all
       or nothing, so a stale-terms 409 or a missing consent rolls the burn back
       and leaves the user able to retry with the same code.
    """
    address = normalize_email(email)
    ip = client_ip(request)
    now = timezone.now()

    # 1. Per-(email, /24) guessing budget. Fails CLOSED (spec §5): an unmetered
    #    verify endpoint is an online oracle against a 10^6 keyspace.
    budget = ratelimit.check_verify_budget(address, ip)
    if not budget.allowed:
        _record_login_failure(request, address, ip, ErrorCode.RATE_LIMITED)
        raise RateLimited(budget.retry_after)

    matched_pk, exhausted = _match_or_charge(address, code, now)

    if matched_pk is None:
        ratelimit.record_verify_failure(address, ip)
        if exhausted:
            logger.info("All live login codes exhausted for one address.")
            error = ApiError(
                ErrorCode.TOO_MANY_ATTEMPTS,
                "Too many wrong attempts for that code. Request a new one.",
                429,
            )
        else:
            error = _classify_failure(address, code, now)
        _record_login_failure(request, address, ip, error.error_code)
        raise error

    return _complete_login(
        request=request,
        address=address,
        ip=ip,
        matched_pk=matched_pk,
        accepts_terms=accepts_terms,
        lang=lang,
        terms_version=terms_version,
        now=now,
    )


@transaction.atomic
def _complete_login(
    *,
    request,
    address: str,
    ip: str | None,
    matched_pk: int,
    accepts_terms: bool,
    lang: str,
    terms_version: str | None,
    now,
) -> tuple[User, bool]:
    """Everything that must happen atomically once the code is known good."""
    # Re-lock and re-check. The row was unlocked when `_match_or_charge`
    # committed, so a concurrent request could have burned it in between;
    # without this, two tabs submitting the same code would both log in and the
    # "single use" guarantee would be a comment rather than a property.
    matched = (
        EmailOTP.objects.filter(pk=matched_pk)
        .select_for_update(of=("self",))
        .first()
    )
    if matched is None or not matched.is_live(now):
        raise ApiError(
            ErrorCode.CODE_EXPIRED, "That code has expired. Request a new one.", 400
        )

    # The code is right. From here on the caller has proved mailbox control, so
    # it is safe to talk about the account.
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

    # Single-use burn.
    matched.consumed_at = now
    matched.save(update_fields=["consumed_at"])

    if created:
        user = User.objects.create_user(
            email=address, lang=effective_lang(lang), is_verified=True
        )
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
        # `lang`, not `effective_lang(lang)`: an explicit choice may overwrite a
        # stored preference, a browser header may not.
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

    # Their own workspace. Idempotent, and (together with `record_security_event`)
    # the only calls this app is allowed to make into apps.tenancy (spec §2).
    from apps.tenancy.services import (  # noqa: PLC0415
        bootstrap_workspace,
        record_security_event,
    )

    membership = bootstrap_workspace(user)

    # Session. `login()` cycles the session key and rotates the CSRF token,
    #    which is what makes session fixation across the anonymous -> logged-in
    #    boundary a non-issue.
    #
    #    It MUST be handed the underlying Django request. `rotate_token()` sets
    #    `request.csrf_cookie_needs_reset = True`, and DRF's Request wrapper
    #    proxies attribute *reads* to the real request but not writes — so
    #    passing the DRF Request would park that flag on the wrapper, where
    #    CsrfViewMiddleware.process_response never looks, and the rotated CSRF
    #    token would silently never reach the browser.
    http_request = getattr(request, "_request", request)
    auth_login(http_request, user, backend=AUTH_BACKEND)
    if http_request is not request:
        # Keep the DRF wrapper in sync; its setter writes through to the real
        # request as well.
        request.user = user

    ratelimit.reset_verify_failures(address, ip)
    logger.info("Login succeeded (new account: %s).", created)
    record_security_event(
        action="auth.login_succeeded",
        user=user,
        org=membership.org,
        ip=ip,
        user_agent=user_agent_of(request),
        target=matched,
    )
    return user, created


# ---------------------------------------------------------------------------
# Trusted devices (docs/2026-08-22-trusted-device-spec.md)
# ---------------------------------------------------------------------------


def trust_this_device(*, request, response, user) -> None:
    """Mint a trusted device for ``user`` and set the cookie on ``response``.

    Called only from the ``verify-code`` view, only when the client sent
    ``remember_device: true``, and only after the code was proven good.

    Never raises. A failure to remember a device must not turn a successful
    login into an error — the user is already authenticated by the time this
    runs, and the worst case is that they type a code again next time.
    """
    from apps.devices.services import set_device_cookie, trust_device  # noqa: PLC0415
    from apps.tenancy.services import record_security_event  # noqa: PLC0415

    ip = client_ip(request)
    agent = user_agent_of(request)
    device = None
    try:
        device, secret = trust_device(user=user, ip=ip, user_agent=agent)
        try:
            set_device_cookie(response, device, secret)
        except Exception:
            # `trust_device` already committed the row; the plaintext secret
            # only ever lived in this process, so the browser has no way to
            # ever present it again. Left alone this is a silent, unrevoked
            # 30-day orphan with no audit trail explaining it — delete it
            # rather than let a cookie-write failure mint an unusable device.
            try:
                device.delete()
            except Exception:
                logger.exception("Also failed to clean up an orphaned trusted device row.")
            raise
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("Failed to remember a trusted device.")
        record_security_event(
            action="auth.device_trust_failed",
            user=user,
            ip=ip,
            user_agent=agent,
            payload={"device_id": str(device.device_id)} if device is not None else None,
        )
        return
    finally:
        # The plaintext secret lives in one process for one request.
        secret = None

    record_security_event(
        action="auth.device_trusted",
        user=user,
        ip=ip,
        user_agent=agent,
        target=device,
    )


# ---------------------------------------------------------------------------
# POST /api/v1/auth/device-login/
# ---------------------------------------------------------------------------


def device_login(*, request, email: str) -> User:
    """Mint a session from a trusted-device cookie. See the trusted-device spec.

    Every rejection is the same ``401 device_not_trusted`` — no cookie, a
    malformed one, an unknown device, a wrong secret, an expired or revoked row,
    an inactive user, or an email that is not the device's owner. The client's
    only correct response to all of them is to fall back to the OTP flow, so a
    finer taxonomy would leak without buying anything (spec §4).

    Unlike ``verify-code`` this never creates an account and never records
    consent: a device can only exist because a full OTP login already did both.
    """
    from apps.devices.services import (  # noqa: PLC0415
        DEVICE_COOKIE_NAME,
        mark_device_used,
        resolve_device,
    )
    from apps.tenancy.services import (  # noqa: PLC0415
        record_security_event,
        resolve_membership,
    )

    address = normalize_email(email)
    ip = client_ip(request)
    agent = user_agent_of(request)

    not_trusted = ApiError(
        ErrorCode.DEVICE_NOT_TRUSTED,
        "This device is not recognised. Please sign in with a code.",
        401,
    )

    def reject(reason: str) -> ApiError:
        ratelimit.record_device_login_failure(address, ip)
        record_security_event(
            action="auth.device_login_failed",
            ip=ip,
            user_agent=agent,
            target_type="email",
            target_id=address,
            payload={"reason": reason},
        )
        return not_trusted

    # No cookie at all is the ordinary "no remembered device" path — most
    # logins take it, not just attackers. It must not touch the hard,
    # fail-closed budget below: that budget exists to blunt cookie-guessing
    # (spec §5), and spending it on every cookie-less login would rate-limit
    # ordinary users out of their own login page, and turn a cache outage into
    # a site-wide login outage the moment the very first visitor arrives.
    if not request.COOKIES.get(DEVICE_COOKIE_NAME):
        raise not_trusted

    # Fails CLOSED, on its own budget (spec §5) — only once a device cookie
    # was actually presented, i.e. there is something to defend against.
    budget = ratelimit.check_device_login_budget(address, ip)
    if not budget.allowed:
        raise RateLimited(budget.retry_after)

    device = resolve_device(request)
    if device is None:
        raise reject("bad_device")

    # The email must name the device's owner. This adds no cryptographic
    # strength — whoever holds the cookie could read the address off the app —
    # but it keeps a deliberate human action in the flow, which is the whole
    # difference between this and a silent auto-login.
    if normalize_email(device.user.email) != address:
        raise reject("email_mismatch")

    user = device.user
    membership = resolve_membership(user, None)

    http_request = getattr(request, "_request", request)
    auth_login(http_request, user, backend=AUTH_BACKEND)
    if http_request is not request:
        request.user = user

    mark_device_used(device, ip=ip, user_agent=agent)
    ratelimit.reset_device_login_failures(address, ip)
    record_security_event(
        action="auth.device_login_succeeded",
        user=user,
        org=membership.org if membership is not None else None,
        ip=ip,
        user_agent=agent,
        target=device,
    )
    logger.info("Trusted-device login succeeded.")
    return user


# ---------------------------------------------------------------------------
# POST /api/v1/auth/logout/
# ---------------------------------------------------------------------------


def logout_user(request) -> None:
    """Audit the logout, then end the session.

    Org resolution has to happen *before* the session is wiped: it reads
    ``request.user`` and the ``?org=``/``X-FindaJob-Org`` the client sent, both
    of which are meaningless once ``auth_logout`` runs.
    """
    from django.contrib.auth import logout as auth_logout  # noqa: PLC0415

    from apps.tenancy.services import current_org, record_security_event  # noqa: PLC0415

    user = request.user
    org = current_org(request)
    record_security_event(
        action="auth.logout",
        user=user,
        org=org,
        ip=client_ip(request),
        user_agent=user_agent_of(request),
    )

    # Same reasoning as in `_complete_login`: `logout()` must see the real
    # Django request, not the DRF wrapper, or session/CSRF state written by it
    # never reaches the response.
    http_request = getattr(request, "_request", request)
    auth_logout(http_request)


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
