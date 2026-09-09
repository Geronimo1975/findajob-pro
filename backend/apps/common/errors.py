"""The one error envelope, shared by every app.

Implementation spec §3::

    Error envelope for every failure: {"error":{"code":"<machine_code>","detail":"<human>"}}

Two keys are guaranteed on every failure response. Extra keys inside ``error``
are *additive* and optional — ``retry_after`` on a 429 and ``fields`` on a
validation error — so a client that only reads ``code`` and ``detail`` never
breaks.

How a failure reaches this module
---------------------------------

=========================================  =========================================
Failure                                    Handler
=========================================  =========================================
Anything raised inside a DRF view          :func:`exception_handler`
URLConf 404 (no route matched)             :func:`api_page_not_found` (``handler404``)
Unhandled exception (real 500)             :func:`api_server_error` (``handler500``)
``SuspiciousOperation`` / bad request       :func:`api_bad_request` (``handler400``)
Django ``PermissionDenied`` outside DRF    :func:`api_permission_denied` (``handler403``)
CSRF rejection                             :func:`csrf_failure` (``CSRF_FAILURE_VIEW``)
=========================================  =========================================

Unhandled exceptions deliberately return ``None`` from
:func:`exception_handler` so DRF re-raises and Django's normal machinery runs
(``django.request`` logging, ``ADMINS`` mail, the debug page under ``DEBUG``).
The JSON envelope is then produced by ``handler500`` instead.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views import defaults as django_defaults
from django.views.csrf import csrf_failure as django_csrf_failure
from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    AuthenticationFailed,
    NotAuthenticated,
    Throttled,
)
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger("findajob.errors")


# ---------------------------------------------------------------------------
# Machine codes
# ---------------------------------------------------------------------------
# The eight auth codes below are pinned by spec §3 and must not be renamed or
# reworded away; `web/api.js` and `web/auth.html` branch on them.


class ErrorCode:
    """Namespace of machine-readable error codes."""

    # --- spec §3, the authentication flow -------------------------------
    INVALID_EMAIL = "invalid_email"
    RATE_LIMITED = "rate_limited"
    INVALID_CODE = "invalid_code"
    CODE_EXPIRED = "code_expired"
    TOO_MANY_ATTEMPTS = "too_many_attempts"
    TERMS_REQUIRED = "terms_required"
    TERMS_OUTDATED = "terms_outdated"
    DELIVERY_UNAVAILABLE = "delivery_unavailable"

    # --- trusted devices (docs/2026-08-22-trusted-device-spec.md §4) -----
    #: The single rejection for `POST /auth/device-login/`. Covers every cause
    #: — no cookie, malformed, unknown, wrong secret, expired, revoked, wrong
    #: owner — because the client's only correct response to all of them is the
    #: same: fall back to the normal OTP flow.
    DEVICE_NOT_TRUSTED = "device_not_trusted"

    # --- transport / framework ------------------------------------------
    NOT_AUTHENTICATED = "not_authenticated"
    PERMISSION_DENIED = "permission_denied"
    NOT_ORG_MEMBER = "not_org_member"
    NOT_FOUND = "not_found"
    METHOD_NOT_ALLOWED = "method_not_allowed"
    PARSE_ERROR = "parse_error"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    INVALID = "invalid"
    BAD_REQUEST = "bad_request"
    CSRF_FAILED = "csrf_failed"
    SERVER_ERROR = "server_error"
    SERVICE_UNAVAILABLE = "service_unavailable"


#: DRF's own ``default_code`` values map cleanly onto ours except where noted.
_DRF_CODE_OVERRIDES = {
    "authentication_failed": ErrorCode.NOT_AUTHENTICATED,
    "not_authenticated": ErrorCode.NOT_AUTHENTICATED,
    "throttled": ErrorCode.RATE_LIMITED,
}

#: Generic wording used when an exception carries no usable message.
_FALLBACK_DETAIL = {
    status.HTTP_400_BAD_REQUEST: "The request could not be processed.",
    status.HTTP_401_UNAUTHORIZED: "Authentication is required.",
    status.HTTP_403_FORBIDDEN: "You do not have permission to do that.",
    status.HTTP_404_NOT_FOUND: "Not found.",
    status.HTTP_405_METHOD_NOT_ALLOWED: "Method not allowed.",
    status.HTTP_429_TOO_MANY_REQUESTS: "Too many requests. Please try again later.",
    status.HTTP_500_INTERNAL_SERVER_ERROR: "Internal server error.",
}


def envelope(code: str, detail: str, **extra: Any) -> dict[str, Any]:
    """Build the canonical failure body."""
    body: dict[str, Any] = {"code": code, "detail": detail}
    body.update({key: value for key, value in extra.items() if value is not None})
    return {"error": body}


# ---------------------------------------------------------------------------
# The exception every app should raise
# ---------------------------------------------------------------------------


class ApiError(APIException):
    """A failure with a pinned machine code.

    ``raise ApiError(ErrorCode.TERMS_REQUIRED, "You must accept the terms.", 400)``

    Subclasses may set :attr:`error_code`, :attr:`status_code` and
    :attr:`default_detail` instead of passing them in.
    """

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = ErrorCode.BAD_REQUEST
    default_detail = "The request could not be processed."

    def __init__(
        self,
        code: str | None = None,
        detail: str | None = None,
        status_code: int | None = None,
        **extra: Any,
    ) -> None:
        self.error_code = code or self.error_code
        if status_code is not None:
            self.status_code = status_code
        self.extra = extra
        super().__init__(detail=detail or self.default_detail, code=self.error_code)


class RateLimited(ApiError):
    """429 with the number of seconds the client must wait."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    error_code = ErrorCode.RATE_LIMITED
    default_detail = "Too many requests. Please try again later."

    def __init__(self, retry_after: int, detail: str | None = None) -> None:
        # Never 0: a client that sleeps for 0 s hammers straight back.
        self.retry_after = max(1, int(retry_after))
        super().__init__(detail=detail, retry_after=self.retry_after)


class NotOrgMember(ApiError):
    """403 — authenticated, but not a member of the workspace being addressed."""

    status_code = status.HTTP_403_FORBIDDEN
    error_code = ErrorCode.NOT_ORG_MEMBER
    default_detail = "You are not a member of this workspace."


class DeliveryUnavailable(ApiError):
    """503 — the email service provider could not be reached in time."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = ErrorCode.DELIVERY_UNAVAILABLE
    default_detail = "We could not send the email right now. Please try again."


# ---------------------------------------------------------------------------
# DRF hook
# ---------------------------------------------------------------------------


def _stringify(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _stringify(item)
            if text:
                return text
        return ""
    if isinstance(value, Mapping):
        for item in value.values():
            text = _stringify(item)
            if text:
                return text
        return ""
    return str(value)


def _flatten(detail: Any) -> tuple[str, dict[str, list[str]] | None]:
    """Reduce a DRF ``detail`` to ``(human_message, field_errors_or_None)``."""
    if isinstance(detail, Mapping):
        if set(detail) == {"detail"}:
            # DRF's own {"detail": "..."} body — a message, not field errors.
            # Without this, an Http404 would grow a nonsensical
            # `fields: {"detail": [...]}` key.
            return _stringify(detail["detail"]), None
        fields: dict[str, list[str]] = {}
        for key, value in detail.items():
            if isinstance(value, (list, tuple)):
                fields[str(key)] = [str(item) for item in value]
            else:
                fields[str(key)] = [str(value)]
        message = _stringify(detail) or "The request could not be processed."
        return message, fields or None
    if isinstance(detail, (list, tuple)):
        return (_stringify(detail) or "The request could not be processed."), None
    return str(detail), None


def _code_for(exc: Exception, response_status: int) -> str:
    explicit = getattr(exc, "error_code", None)
    if explicit:
        return str(explicit)
    detail = getattr(exc, "detail", None)
    raw = getattr(detail, "code", None) or getattr(exc, "default_code", None)
    if raw:
        raw = str(raw)
        return _DRF_CODE_OVERRIDES.get(raw, raw)
    return {
        status.HTTP_400_BAD_REQUEST: ErrorCode.BAD_REQUEST,
        status.HTTP_401_UNAUTHORIZED: ErrorCode.NOT_AUTHENTICATED,
        status.HTTP_403_FORBIDDEN: ErrorCode.PERMISSION_DENIED,
        status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
        status.HTTP_405_METHOD_NOT_ALLOWED: ErrorCode.METHOD_NOT_ALLOWED,
        status.HTTP_429_TOO_MANY_REQUESTS: ErrorCode.RATE_LIMITED,
    }.get(response_status, ErrorCode.SERVER_ERROR)


def exception_handler(exc, context):
    """DRF ``EXCEPTION_HANDLER``: rewrite every handled failure into the envelope."""
    response = drf_exception_handler(exc, context)
    if response is None:
        # Not an API-level failure — a genuine bug. Let DRF re-raise so Django
        # logs it, mails ADMINS and renders the debug page under DEBUG; the
        # JSON envelope for the client then comes from handler500.
        return None

    # DRF downgrades 401 -> 403 whenever the authentication class offers no
    # `WWW-Authenticate` header. `web/app.html` boots by calling GET /api/v1/me/
    # and redirects to /auth.html on 401 (spec §7), so the distinction is
    # load-bearing: an authenticated-but-forbidden user must still get 403.
    if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
        response.status_code = status.HTTP_401_UNAUTHORIZED

    detail_source = getattr(exc, "detail", None)
    if detail_source is None:
        detail_source = response.data if response.data is not None else ""
    message, fields = _flatten(detail_source)
    if not message:
        message = _FALLBACK_DETAIL.get(response.status_code, "Request failed.")

    extra: dict[str, Any] = dict(getattr(exc, "extra", {}) or {})
    if fields:
        extra.setdefault("fields", fields)

    retry_after = extra.get("retry_after")
    if retry_after is None and isinstance(exc, Throttled) and exc.wait is not None:
        # DRF's own Throttled carries `wait`; ours carries `retry_after`. Both
        # must surface, and neither may ever raise AttributeError (spec §12).
        retry_after = max(1, int(exc.wait))
        extra["retry_after"] = retry_after

    response.data = envelope(
        _code_for(exc, response.status_code), message, **extra
    )
    if retry_after is not None:
        response["Retry-After"] = str(int(retry_after))
    return response


# ---------------------------------------------------------------------------
# Django-level handlers (registered in config/urls.py)
# ---------------------------------------------------------------------------

_API_PREFIX = "/api/"


def _wants_json(request: HttpRequest) -> bool:
    path = getattr(request, "path", "") or ""
    if path.startswith(_API_PREFIX):
        return True
    accept = request.META.get("HTTP_ACCEPT", "") if hasattr(request, "META") else ""
    return "application/json" in accept and "text/html" not in accept


def _json_error(code: str, detail: str, http_status: int, **extra: Any) -> JsonResponse:
    return JsonResponse(envelope(code, detail, **extra), status=http_status)


def api_page_not_found(request: HttpRequest, exception=None) -> HttpResponse:
    """``handler404`` — JSON under ``/api/``, Django's HTML page elsewhere."""
    if _wants_json(request):
        return _json_error(ErrorCode.NOT_FOUND, "Not found.", 404)
    return django_defaults.page_not_found(request, exception)


def api_server_error(request: HttpRequest) -> HttpResponse:
    """``handler500`` — never leaks the exception message to the client."""
    if _wants_json(request):
        return _json_error(
            ErrorCode.SERVER_ERROR,
            "Internal server error.",
            500,
        )
    return django_defaults.server_error(request)


def api_bad_request(request: HttpRequest, exception=None) -> HttpResponse:
    """``handler400`` — ``SuspiciousOperation``, bad Host header, and friends."""
    if _wants_json(request):
        return _json_error(ErrorCode.BAD_REQUEST, "Bad request.", 400)
    return django_defaults.bad_request(request, exception)


def api_permission_denied(request: HttpRequest, exception=None) -> HttpResponse:
    """``handler403`` — Django-level ``PermissionDenied`` raised outside DRF."""
    if _wants_json(request):
        return _json_error(
            ErrorCode.PERMISSION_DENIED, "You do not have permission to do that.", 403
        )
    return django_defaults.permission_denied(request, exception)


def csrf_failure(request: HttpRequest, reason: str = "", template_name: str = "") -> HttpResponse:
    """``CSRF_FAILURE_VIEW``.

    Both anonymous auth POSTs carry ``@csrf_protect`` (spec §8), so this is the
    response a client sees when it forgets the ``X-CSRFToken`` header. The
    reason string is logged, never returned: it describes our own defences.
    """
    if _wants_json(request):
        logger.warning(
            "CSRF failure on %s: %s", getattr(request, "path", "?"), reason
        )
        return _json_error(
            ErrorCode.CSRF_FAILED,
            "CSRF verification failed. Reload the page and try again.",
            403,
        )
    return django_csrf_failure(request, reason=reason, template_name=template_name or "403_csrf.html")
