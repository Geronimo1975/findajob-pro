"""The boot-time checks that stand between a misconfigured deploy and silence.

Spec §6: *"An empty mail config must never produce a green UI and zero delivered
codes."* These checks are the only thing enforcing that, so a regression here is
invisible until nobody can log in. Each test asserts both directions — the error
fires when it should, and stays quiet when the configuration is sound.
"""

from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from config.checks import (
    E_CONSOLE_BACKEND,
    E_LOCAL_CACHE,
    E_SMTP_INCOMPLETE,
    check_email_delivery,
    check_shared_cache,
)

SMTP = "django.core.mail.backends.smtp.EmailBackend"
CONSOLE = "django.core.mail.backends.console.EmailBackend"
LOCMEM_CACHE = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
REDIS_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": "redis://127.0.0.1:6379/0",
    }
}

COMPLETE_SMTP = dict(
    DEBUG=False,
    EMAIL_CHECK_EXEMPT=False,
    EMAIL_BACKEND=SMTP,
    EMAIL_HOST="smtp-relay.brevo.com",
    EMAIL_PORT=587,
    EMAIL_USE_TLS=True,
    EMAIL_USE_SSL=False,
    EMAIL_HOST_USER="relay@findajob.pro",
    EMAIL_HOST_PASSWORD="an-smtp-key",
    DEFAULT_FROM_EMAIL="findajob.pro <noreply@mail.findajob.pro>",
)


def _ids(errors):
    return {e.id for e in errors}


class EmailDeliveryCheckTests(SimpleTestCase):
    def test_complete_smtp_configuration_is_accepted(self):
        with override_settings(**COMPLETE_SMTP):
            self.assertEqual(check_email_delivery(None), [])

    def test_missing_credentials_are_an_error(self):
        for blank in ("EMAIL_HOST_USER", "EMAIL_HOST_PASSWORD"):
            with self.subTest(blank=blank):
                with override_settings(**{**COMPLETE_SMTP, blank: ""}):
                    errors = check_email_delivery(None)
                self.assertIn(E_SMTP_INCOMPLETE, _ids(errors))
                self.assertIn(blank, errors[0].msg)

    def test_localhost_host_is_an_error(self):
        # An empty EMAIL_HOST means SMTP to localhost inside the container,
        # where nothing listens — the exact "green UI, zero delivery" failure.
        for host in ("", "localhost", "127.0.0.1"):
            with self.subTest(host=host):
                with override_settings(**{**COMPLETE_SMTP, "EMAIL_HOST": host}):
                    self.assertIn(E_SMTP_INCOMPLETE, _ids(check_email_delivery(None)))

    def test_submission_port_without_encryption_is_an_error(self):
        for port in (587, 465):
            with self.subTest(port=port):
                with override_settings(
                    **{
                        **COMPLETE_SMTP,
                        "EMAIL_PORT": port,
                        "EMAIL_USE_TLS": False,
                        "EMAIL_USE_SSL": False,
                    }
                ):
                    self.assertIn(E_SMTP_INCOMPLETE, _ids(check_email_delivery(None)))

    def test_ssl_on_465_is_accepted(self):
        with override_settings(
            **{**COMPLETE_SMTP, "EMAIL_PORT": 465, "EMAIL_USE_TLS": False, "EMAIL_USE_SSL": True}
        ):
            self.assertEqual(check_email_delivery(None), [])

    def test_console_backend_outside_debug_is_still_an_error(self):
        with override_settings(**{**COMPLETE_SMTP, "EMAIL_BACKEND": CONSOLE}):
            self.assertIn(E_CONSOLE_BACKEND, _ids(check_email_delivery(None)))

    def test_debug_and_the_test_exemption_silence_everything(self):
        for kwargs in ({"DEBUG": True}, {"EMAIL_CHECK_EXEMPT": True}):
            with self.subTest(**kwargs):
                with override_settings(
                    **{**COMPLETE_SMTP, "EMAIL_HOST_USER": "", **kwargs}
                ):
                    self.assertEqual(check_email_delivery(None), [])


class SharedCacheCheckTests(SimpleTestCase):
    def test_per_process_cache_outside_debug_is_an_error(self):
        # LocMem means every gunicorn worker keeps its own rate-limit budget, so
        # the spec §5 ceilings silently multiply by the worker count.
        with override_settings(DEBUG=False, EMAIL_CHECK_EXEMPT=False, CACHES=LOCMEM_CACHE):
            errors = check_shared_cache(None)
        self.assertEqual(_ids(errors), {E_LOCAL_CACHE})
        self.assertIn("REDIS_URL", errors[0].hint)

    def test_shared_cache_is_accepted(self):
        with override_settings(DEBUG=False, EMAIL_CHECK_EXEMPT=False, CACHES=REDIS_CACHE):
            self.assertEqual(check_shared_cache(None), [])

    def test_debug_silences_it(self):
        with override_settings(DEBUG=True, CACHES=LOCMEM_CACHE):
            self.assertEqual(check_shared_cache(None), [])
