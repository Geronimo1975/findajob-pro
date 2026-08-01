"""Project-level contracts other work streams build on.

Every assertion here is something ``apps/accounts``, ``apps/legal`` or
``web/api.js`` depends on and that would fail *silently* if it drifted:
a swapped user model, a browsable renderer leaking into production, a
``HttpOnly`` CSRF cookie that JavaScript can no longer read.
"""

from __future__ import annotations

import importlib
import os
import sys
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from django.urls import get_resolver


class UserModelContractTests(SimpleTestCase):
    def test_auth_user_model_is_pinned_before_first_migrate(self):
        """Spec §2: there is exactly one chance at this."""
        self.assertEqual(settings.AUTH_USER_MODEL, "accounts.User")


class InstalledAppsContractTests(SimpleTestCase):
    def test_the_three_local_apps_are_installed(self):
        for app in ("apps.tenancy", "apps.accounts", "apps.legal"):
            with self.subTest(app=app):
                self.assertIn(app, settings.INSTALLED_APPS)

    def test_no_unbuilt_module_is_installed(self):
        """Build-order positions 5+ must not sneak in."""
        for app in settings.INSTALLED_APPS:
            with self.subTest(app=app):
                self.assertNotIn("billing", app)
                self.assertNotIn("org_members", app)
                self.assertNotIn("entitlements", app)

    def test_rest_framework_and_anymail_are_installed(self):
        self.assertIn("rest_framework", settings.INSTALLED_APPS)
        self.assertIn("anymail", settings.INSTALLED_APPS)


class DrfContractTests(SimpleTestCase):
    def test_session_authentication_only(self):
        self.assertEqual(
            settings.REST_FRAMEWORK["DEFAULT_AUTHENTICATION_CLASSES"],
            ["apps.common.authentication.CookieSessionAuthentication"],
        )

    def test_default_permission_is_is_authenticated(self):
        """Spec §7: the gate is the API — a new view is protected by default."""
        self.assertEqual(
            settings.REST_FRAMEWORK["DEFAULT_PERMISSION_CLASSES"],
            ["rest_framework.permissions.IsAuthenticated"],
        )

    def test_exception_handler_is_the_shared_envelope(self):
        self.assertEqual(
            settings.REST_FRAMEWORK["EXCEPTION_HANDLER"],
            "apps.common.errors.exception_handler",
        )

    def test_no_browsable_renderer_outside_development(self):
        self.assertEqual(
            settings.REST_FRAMEWORK["DEFAULT_RENDERER_CLASSES"],
            ["rest_framework.renderers.JSONRenderer"],
        )

    def test_401_carries_a_www_authenticate_header(self):
        from apps.common.authentication import CookieSessionAuthentication

        header = CookieSessionAuthentication().authenticate_header(None)
        self.assertTrue(header)
        self.assertNotIn("Basic", header, "must not trigger a browser login prompt")


class CookieContractTests(SimpleTestCase):
    def test_csrf_cookie_stays_readable_by_javascript(self):
        """web/api.js reads document.cookie to build X-CSRFToken (spec §8)."""
        self.assertFalse(settings.CSRF_COOKIE_HTTPONLY)

    def test_session_cookie_is_httponly(self):
        self.assertTrue(settings.SESSION_COOKIE_HTTPONLY)

    def test_samesite_is_lax_on_both_cookies(self):
        self.assertEqual(settings.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertEqual(settings.CSRF_COOKIE_SAMESITE, "Lax")

    def test_sessions_live_in_the_database_not_redis(self):
        """Redis is cache-only: an outage must not log everybody out."""
        self.assertEqual(settings.SESSION_ENGINE, "django.contrib.sessions.backends.db")

    def test_csrf_failure_returns_the_shared_envelope(self):
        self.assertEqual(
            settings.CSRF_FAILURE_VIEW, "apps.common.errors.csrf_failure"
        )


class I18nContractTests(SimpleTestCase):
    def test_three_languages_en_default(self):
        self.assertEqual(settings.LANGUAGE_CODE, "en")
        self.assertEqual(
            [code for code, _label in settings.LANGUAGES], ["en", "de", "ro"]
        )
        self.assertEqual(settings.SUPPORTED_LANGUAGE_CODES, ("en", "de", "ro"))

    def test_locale_paths_are_configured(self):
        self.assertTrue(settings.LOCALE_PATHS)

    def test_time_is_utc_and_aware(self):
        self.assertEqual(settings.TIME_ZONE, "UTC")
        self.assertTrue(settings.USE_TZ)

    def test_lang_normalisation(self):
        from apps.tenancy.serializers import normalise_lang

        self.assertEqual(normalise_lang("de"), "de")
        self.assertEqual(normalise_lang("DE"), "de")
        self.assertEqual(normalise_lang("de-AT"), "de")
        self.assertEqual(normalise_lang("ro"), "ro")
        self.assertEqual(normalise_lang(None), "en")
        self.assertEqual(normalise_lang(""), "en")
        self.assertEqual(normalise_lang("fr"), "en")
        self.assertEqual(normalise_lang(12345), "en")


class UrlContractTests(SimpleTestCase):
    def test_everything_lives_under_api_v1(self):
        """web/api.js hardcodes BASE = '/api/v1'."""
        from django.urls import reverse

        self.assertTrue(reverse("tenancy:me").startswith("/api/v1/"))

    def test_error_handlers_are_wired(self):
        resolver = get_resolver()
        self.assertEqual(
            resolver.urlconf_module.handler404, "apps.common.errors.api_page_not_found"
        )
        self.assertEqual(
            resolver.urlconf_module.handler500, "apps.common.errors.api_server_error"
        )
        self.assertEqual(
            resolver.urlconf_module.handler400, "apps.common.errors.api_bad_request"
        )
        self.assertEqual(
            resolver.urlconf_module.handler403,
            "apps.common.errors.api_permission_denied",
        )


class ProductionSettingsTests(SimpleTestCase):
    """``config.settings.prod`` is never the active module under test, so its
    hardening is verified by importing it with an explicit environment."""

    ENV = {
        "SECRET_KEY": "a-genuinely-unique-production-secret-key-for-tests-0123456789",
        "DEBUG": "False",
        "ALLOWED_HOSTS": "findajob.pro,www.findajob.pro",
        "CSRF_TRUSTED_ORIGINS": "https://findajob.pro,https://www.findajob.pro",
        "EMAIL_BACKEND": "anymail.backends.brevo.EmailBackend",
        "BREVO_API_KEY": "xkeysib-test",
    }

    def load_prod(self):
        # `config.settings.prod` does `from config.settings.base import *`, so
        # base has to be re-executed too or its values come from the cached
        # module (computed under the test environment).
        stale = ("config.settings.prod", "config.settings.base")
        saved = {name: sys.modules.pop(name, None) for name in stale}
        try:
            with mock.patch.dict(os.environ, self.ENV):
                return importlib.import_module("config.settings.prod")
        finally:
            for name in stale:
                sys.modules.pop(name, None)
                if saved[name] is not None:
                    sys.modules[name] = saved[name]

    def test_debug_is_off(self):
        self.assertIs(self.load_prod().DEBUG, False)

    def test_proxy_ssl_header_for_traefik(self):
        prod = self.load_prod()
        self.assertEqual(
            prod.SECURE_PROXY_SSL_HEADER, ("HTTP_X_FORWARDED_PROTO", "https")
        )

    def test_cookies_are_secure(self):
        prod = self.load_prod()
        self.assertIs(prod.SESSION_COOKIE_SECURE, True)
        self.assertIs(prod.SESSION_COOKIE_HTTPONLY, True)
        self.assertEqual(prod.SESSION_COOKIE_SAMESITE, "Lax")
        self.assertIs(prod.CSRF_COOKIE_SECURE, True)
        self.assertIs(
            prod.CSRF_COOKIE_HTTPONLY, False, "api.js must still read csrftoken"
        )

    def test_hsts_and_clickjacking(self):
        prod = self.load_prod()
        self.assertGreaterEqual(prod.SECURE_HSTS_SECONDS, 31_536_000)
        self.assertIs(prod.SECURE_HSTS_INCLUDE_SUBDOMAINS, True)
        self.assertIs(prod.SECURE_HSTS_PRELOAD, True)
        self.assertEqual(prod.X_FRAME_OPTIONS, "DENY")
        self.assertIs(prod.SECURE_CONTENT_TYPE_NOSNIFF, True)

    def test_hosts_and_csrf_origins_come_from_the_environment(self):
        prod = self.load_prod()
        self.assertEqual(prod.ALLOWED_HOSTS, ["findajob.pro", "www.findajob.pro"])
        self.assertEqual(
            prod.CSRF_TRUSTED_ORIGINS,
            ["https://findajob.pro", "https://www.findajob.pro"],
        )

    def test_static_files_use_a_manifest_store(self):
        prod = self.load_prod()
        self.assertEqual(
            prod.STORAGES["staticfiles"]["BACKEND"],
            "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
        )
