"""The four ``/api/v1/auth/*`` endpoints (implementation spec §3).

======  ==========================  =========================  ===============
Method  Path                        Auth                       Success
======  ==========================  =========================  ===============
GET     ``/auth/csrf/``             anon                       ``200 {"detail":"ok"}``
POST    ``/auth/request-code/``     anon, ``@csrf_protect``    ``202``
POST    ``/auth/verify-code/``      anon, ``@csrf_protect``    ``200`` + session
POST    ``/auth/logout/``           session                    ``204``
======  ==========================  =========================  ===============

There is deliberately **no** ``/auth/me/`` and no ``/auth/session/`` here. The
single session endpoint is ``GET /api/v1/me/`` and it belongs to
``apps.tenancy``.

Why ``@method_decorator(csrf_protect, name="dispatch")`` appears explicitly
-------------------------------------------------------------------------

``APIView.as_view()`` wraps itself in ``csrf_exempt``, which stops
``CsrfViewMiddleware`` from ever processing the view. DRF's own CSRF check lives
in ``SessionAuthentication.enforce_csrf`` and therefore runs **only for a
request that already carries a session cookie** — which an anonymous login POST
does not. Without the decorator below, ``request-code`` and ``verify-code``
would accept cross-site POSTs: an attacker's page could log a visitor into an
account the attacker controls, and could burn a victim's rate-limit budget at
will. This is spec §8's *"without it, login CSRF is live"*.

Thin views on purpose
---------------------

Everything these do is: validate, translate field errors into the pinned machine
codes, call :mod:`apps.accounts.services`, and shape the response. No branching
on account existence lives here — see the ``request-code`` note below.
"""

from __future__ import annotations

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.serializers import RequestCodeSerializer, VerifyCodeSerializer
from apps.accounts.serializers_device import DeviceLoginSerializer
from apps.accounts.services import (
    device_login,
    logout_user,
    request_login_code,
    verify_login_code,
)
from apps.common.errors import ApiError, ErrorCode


def _me_payload(request, user) -> dict:
    """The ``GET /api/v1/me/`` body, so a login can redirect without a round trip.

    Imported lazily, not at module import time: ``apps.tenancy`` is a sibling
    app and a top-level import would make the two packages load in a fixed order
    for no benefit. The rule this respects is spec §2 — accounts may call
    tenancy's *services and serializers*, never its models.
    """
    from apps.tenancy.serializers import MeSerializer  # noqa: PLC0415
    from apps.tenancy.services import memberships_for, resolve_membership  # noqa: PLC0415

    memberships = list(memberships_for(user))
    membership = resolve_membership(user, None, memberships=memberships)
    return MeSerializer(
        user,
        context={
            "request": request,
            "memberships": memberships,
            "membership": membership,
        },
    ).data


def _reject(serializer, *, email_code: str, fallback_code: str) -> ApiError:
    """Turn DRF field errors into one pinned machine code (spec §3).

    ``web/auth.html`` branches on the code and nothing else, so a serializer
    error has to arrive as ``invalid_email`` or ``invalid_code`` — never as
    DRF's generic ``invalid`` with a ``fields`` map the page cannot read.
    """
    errors = serializer.errors
    if "email" in errors:
        return ApiError(
            email_code, "That does not look like a valid email address.", 400
        )
    if "code" in errors:
        return ApiError(fallback_code, "That code is not correct.", 400)
    return ApiError(ErrorCode.BAD_REQUEST, "The request could not be processed.", 400)


@method_decorator(ensure_csrf_cookie, name="dispatch")
class CsrfView(APIView):
    """``GET /api/v1/auth/csrf/`` — hand the browser a CSRF token.

    ``web/api.js``'s ``bootstrapCsrf()`` awaits this on load of every page that
    can POST (spec §8). Without it the very first login POST would be rejected
    for a token the browser has never been given.

    ``ensure_csrf_cookie`` is needed even though ``CsrfViewMiddleware`` is
    installed: DRF's ``csrf_exempt`` keeps the middleware's ``process_view``
    from running, and nothing else on this response would call ``get_token()``.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        return Response({"detail": "ok"})


@method_decorator(csrf_protect, name="dispatch")
class RequestCodeView(APIView):
    """``POST /api/v1/auth/request-code/`` — ``{email, lang}`` -> ``202``.

    **Branch-free** (spec §6). This view never learns whether the address has an
    account, because nothing in the path it calls queries ``User``. The status,
    the body, the field order and the work done are identical either way, so a
    caller cannot use it to enumerate registered users — not by reading the
    response and not by timing it.

    Two failures are still possible and both are honest:
    ``429 rate_limited`` with a ``retry_after``, and ``503
    delivery_unavailable`` when the provider could not be reached. Neither
    depends on whether the account exists.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = RequestCodeSerializer(data=request.data)
        if not serializer.is_valid():
            raise _reject(
                serializer,
                email_code=ErrorCode.INVALID_EMAIL,
                fallback_code=ErrorCode.INVALID_EMAIL,
            )

        payload = request_login_code(
            request=request,
            email=serializer.validated_data["email"],
            lang=serializer.validated_data.get("lang") or "",
        )
        return Response(payload, status=status.HTTP_202_ACCEPTED)


@method_decorator(csrf_protect, name="dispatch")
class VerifyCodeView(APIView):
    """``POST /api/v1/auth/verify-code/`` — ``{email, code, accepts_terms}``.

    On success: the session cookie is set and the body is the same shape
    ``GET /api/v1/me/`` returns, so ``web/auth.html`` can redirect straight into
    the app without a second round trip. That shape is produced by
    ``apps.tenancy``'s serializer, which keeps the contract in one place.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = VerifyCodeSerializer(data=request.data)
        if not serializer.is_valid():
            raise _reject(
                serializer,
                email_code=ErrorCode.INVALID_EMAIL,
                fallback_code=ErrorCode.INVALID_CODE,
            )

        data = serializer.validated_data
        user, created = verify_login_code(
            request=request,
            email=data["email"],
            code=data["code"],
            accepts_terms=bool(data.get("accepts_terms")),
            lang=data.get("lang") or "",
            terms_version=data.get("terms_version") or "",
        )

        payload = _me_payload(request, user)
        payload["created"] = created
        response = Response(payload, status=status.HTTP_200_OK)

        if data.get("remember_device"):
            # Trusted-device spec §6. Minted only on an explicit opt-in, and
            # only after the code was already proven good.
            from apps.accounts.services import trust_this_device  # noqa: PLC0415

            trust_this_device(request=request, response=response, user=user)

        return response


@method_decorator(csrf_protect, name="dispatch")
class DeviceLoginView(APIView):
    """``POST /api/v1/auth/device-login/`` — ``{email}`` -> ``200`` + session.

    The fifth auth route, and a deliberate deviation from auth spec §3's four;
    ``docs/2026-08-22-trusted-device-spec.md`` §7 records why. The short version:
    the alternative was to let ``request-code`` sometimes return a session, and
    ``request-code`` being branch-free is the most security-critical property in
    this module.

    On any rejection the stale cookie is cleared, so a browser holding a revoked
    or expired device stops presenting it on every subsequent login.
    """

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = DeviceLoginSerializer(data=request.data)
        if not serializer.is_valid():
            raise _reject(
                serializer,
                email_code=ErrorCode.INVALID_EMAIL,
                fallback_code=ErrorCode.INVALID_EMAIL,
            )

        from apps.devices.services import clear_device_cookie  # noqa: PLC0415

        try:
            user = device_login(
                request=request, email=serializer.validated_data["email"]
            )
        except ApiError as exc:
            if exc.error_code != ErrorCode.DEVICE_NOT_TRUSTED:
                raise
            response = Response(
                {"error": {"code": exc.error_code, "detail": str(exc.detail)}},
                status=exc.status_code,
            )
            clear_device_cookie(response)
            return response

        return Response(_me_payload(request, user), status=status.HTTP_200_OK)


@method_decorator(csrf_protect, name="dispatch")
class LogoutView(APIView):
    """``POST /api/v1/auth/logout/`` -> ``204``.

    CSRF is enforced twice over: by ``SessionAuthentication.enforce_csrf`` (this
    is a cookie-authenticated request) and by the explicit decorator. Belt and
    braces, because a logout CSRF is a real, if minor, nuisance attack.

    All of the actual work — resolving the org for the audit trail, then
    ending the session — lives in ``apps.accounts.services.logout_user``; see
    that docstring for why the ordering matters.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        logout_user(request)
        return Response(status=status.HTTP_204_NO_CONTENT)
