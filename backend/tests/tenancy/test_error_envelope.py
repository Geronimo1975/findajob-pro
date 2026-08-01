"""The shared error envelope and the boot-time deploy checks.

Envelope contract (implementation spec §3)::

    {"error": {"code": "<machine_code>", "detail": "<human>"}}

Every failure that can reach a browser has to look like that, including the
ones DRF never sees: URLConf 404s, CSRF rejections and unhandled 500s.
"""

from __future__ import annotations

import json

from django.core.checks import Error
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.http import Http404
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework import status
from rest_framework.exceptions import (
    MethodNotAllowed,
    NotAuthenticated,
    NotFound,
    PermissionDenied,
    Throttled,
    ValidationError,
)

from apps.common.errors import (
    ApiError,
    DeliveryUnavailable,
    ErrorCode,
    RateLimited,
    envelope,
    exception_handler,
)
from apps.common.logging import redact
from config import checks


class EnvelopeShapeTests(SimpleTestCase):
    def test_envelope_has_exactly_code_and_detail_by_default(self):
        body = envelope(ErrorCode.INVALID_EMAIL, "That address looks wrong.")
        self.assertEqual(body, {"error": {"code": "invalid_email", "detail": "That address looks wrong."}})

    def test_extra_keys_are_additive(self):
        body = envelope(ErrorCode.RATE_LIMITED, "Slow down.", retry_after=42)
        self.assertEqual(body["error"]["code"], "rate_limited")
        self.assertEqual(body["error"]["retry_after"], 42)

    def test_none_valued_extras_are_dropped(self):
        body = envelope(ErrorCode.INVALID, "nope", retry_after=None)
        self.assertEqual(set(body["error"]), {"code", "detail"})

    def test_every_spec_code_exists(self):
        """Spec §3 pins these eight names; renaming one breaks web/auth.html."""
        for code in (
            "invalid_email",
            "rate_limited",
            "invalid_code",
            "code_expired",
            "too_many_attempts",
            "terms_required",
            "terms_outdated",
            "delivery_unavailable",
        ):
            with self.subTest(code=code):
                self.assertIn(
                    code,
                    {
                        value
                        for name, value in vars(ErrorCode).items()
                        if not name.startswith("_") and isinstance(value, str)
                    },
                )


class ExceptionHandlerTests(SimpleTestCase):
    def setUp(self):
        self.context = {"request": RequestFactory().get("/api/v1/me/"), "view": None}

    def handle(self, exc):
        response = exception_handler(exc, self.context)
        self.assertIsNotNone(response, "the handler must produce a response")
        return response

    def test_api_error_carries_its_code(self):
        response = self.handle(
            ApiError(ErrorCode.TERMS_REQUIRED, "You must accept the terms.", 400)
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["code"], "terms_required")
        self.assertEqual(response.data["error"]["detail"], "You must accept the terms.")

    def test_not_authenticated_is_401_not_403(self):
        response = self.handle(NotAuthenticated())
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(response.data["error"]["code"], "not_authenticated")

    def test_permission_denied_stays_403(self):
        response = self.handle(PermissionDenied())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data["error"]["code"], "permission_denied")

    def test_not_found_and_method_not_allowed(self):
        self.assertEqual(self.handle(NotFound()).data["error"]["code"], "not_found")
        self.assertEqual(
            self.handle(MethodNotAllowed("POST")).data["error"]["code"],
            "method_not_allowed",
        )

    def test_django_http404_and_permission_denied_are_translated(self):
        """DRF converts these before we see them; the envelope must survive."""
        response = self.handle(Http404())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.data["error"]["code"], "not_found")
        self.assertEqual(set(response.data["error"]), {"code", "detail"})

        response = self.handle(DjangoPermissionDenied())
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.data["error"]["code"], "permission_denied")
        self.assertEqual(set(response.data["error"]), {"code", "detail"})

    def test_validation_errors_flatten_into_detail_plus_fields(self):
        response = self.handle(ValidationError({"email": ["Enter a valid email."]}))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["error"]["detail"], "Enter a valid email.")
        self.assertEqual(
            response.data["error"]["fields"], {"email": ["Enter a valid email."]}
        )

    def test_rate_limited_carries_retry_after_and_the_header(self):
        response = self.handle(RateLimited(60))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["error"]["code"], "rate_limited")
        self.assertEqual(response.data["error"]["retry_after"], 60)
        self.assertEqual(response["Retry-After"], "60")

    def test_retry_after_is_never_zero(self):
        self.assertEqual(RateLimited(0).retry_after, 1)
        self.assertEqual(RateLimited(-5).retry_after, 1)

    def test_drf_throttled_also_gets_retry_after(self):
        """Spec §12: never an AttributeError on the rate-limit path."""
        response = self.handle(Throttled(wait=30))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.data["error"]["code"], "rate_limited")
        self.assertEqual(response.data["error"]["retry_after"], 30)
        self.assertEqual(response["Retry-After"], "30")

    def test_drf_throttled_without_a_wait_does_not_explode(self):
        response = self.handle(Throttled())
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("retry_after", response.data["error"])

    def test_delivery_unavailable_is_503(self):
        response = self.handle(DeliveryUnavailable())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.data["error"]["code"], "delivery_unavailable")

    def test_unhandled_exceptions_are_not_swallowed(self):
        """Returning None lets Django log, mail ADMINS and render the debug page."""
        self.assertIsNone(exception_handler(RuntimeError("boom"), self.context))


class DjangoLevelHandlerTests(TestCase):
    def test_unknown_api_route_returns_the_envelope(self):
        response = self.client.get("/api/v1/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_unknown_non_api_route_still_returns_html(self):
        response = self.client.get("/definitely-not-here")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("application/json", response["Content-Type"])

    def test_csrf_failure_on_an_api_path_returns_the_envelope(self):
        from apps.common.errors import csrf_failure

        request = RequestFactory().post("/api/v1/auth/verify-code/")
        response = csrf_failure(request, reason="CSRF cookie not set.")

        self.assertEqual(response.status_code, 403)
        body = json.loads(response.content)
        self.assertEqual(body["error"]["code"], "csrf_failed")
        self.assertNotIn("CSRF cookie not set", body["error"]["detail"])


class LogRedactionTests(SimpleTestCase):
    def test_codes_and_secrets_never_survive_a_log_line(self):
        for line in (
            "issuing code=123456 for a@b.de",
            'payload {"code": "654321"}',
            "Authorization: Bearer abcdef",
            "api_key=sk-ant-123",
        ):
            with self.subTest(line=line):
                cleaned = redact(line)
                for secret in ("123456", "654321", "abcdef", "sk-ant-123"):
                    self.assertNotIn(secret, cleaned)


class DeployCheckTests(SimpleTestCase):
    """Spec §6 — an empty mail config must never produce a green UI."""

    def ids(self, results):
        return {result.id for result in results if isinstance(result, Error)}

    @override_settings(DEBUG=True, EMAIL_CHECK_EXEMPT=False)
    def test_silent_while_debugging(self):
        self.assertEqual(checks.check_email_delivery(None), [])

    @override_settings(
        DEBUG=False,
        EMAIL_CHECK_EXEMPT=False,
        EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@findajob.pro",
    )
    def test_console_backend_in_production_is_an_error(self):
        self.assertIn(checks.E_CONSOLE_BACKEND, self.ids(checks.check_email_delivery(None)))

    @override_settings(
        DEBUG=False,
        EMAIL_CHECK_EXEMPT=False,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="noreply@findajob.pro",
    )
    def test_locmem_backend_in_production_is_an_error(self):
        self.assertIn(checks.E_CONSOLE_BACKEND, self.ids(checks.check_email_delivery(None)))

    @override_settings(
        DEBUG=False,
        EMAIL_CHECK_EXEMPT=False,
        EMAIL_BACKEND="anymail.backends.brevo.EmailBackend",
        ANYMAIL={"BREVO_API_KEY": "   "},
        DEFAULT_FROM_EMAIL="noreply@findajob.pro",
    )
    def test_empty_brevo_key_is_an_error(self):
        self.assertIn(checks.E_EMPTY_ESP_KEY, self.ids(checks.check_email_delivery(None)))

    @override_settings(
        DEBUG=False,
        EMAIL_CHECK_EXEMPT=False,
        EMAIL_BACKEND="anymail.backends.brevo.EmailBackend",
        ANYMAIL={"BREVO_API_KEY": "xkeysib-real-looking-key"},
        DEFAULT_FROM_EMAIL="",
    )
    def test_empty_from_address_is_an_error(self):
        self.assertIn(checks.E_NO_FROM_ADDRESS, self.ids(checks.check_email_delivery(None)))

    @override_settings(
        DEBUG=False,
        EMAIL_CHECK_EXEMPT=False,
        EMAIL_BACKEND="anymail.backends.brevo.EmailBackend",
        ANYMAIL={"BREVO_API_KEY": "xkeysib-real-looking-key"},
        DEFAULT_FROM_EMAIL="noreply@findajob.pro",
    )
    def test_a_complete_configuration_passes(self):
        self.assertEqual(checks.check_email_delivery(None), [])

    @override_settings(DEBUG=False, EMAIL_CHECK_EXEMPT=True)
    def test_the_test_runners_locmem_backend_is_exempt(self):
        self.assertEqual(checks.check_email_delivery(None), [])

    @override_settings(
        DEBUG=False,
        SECRET_KEY="django-insecure-findajob-local-development-key-do-not-ship",
        INSECURE_DEV_SECRET_KEY="django-insecure-findajob-local-development-key-do-not-ship",
    )
    def test_development_secret_key_in_production_is_an_error(self):
        self.assertIn(checks.E_INSECURE_SECRET_KEY, self.ids(checks.check_secret_key(None)))

    @override_settings(DEBUG=False, SECRET_KEY="a-real-and-unique-production-key")
    def test_a_real_secret_key_passes(self):
        self.assertEqual(checks.check_secret_key(None), [])
