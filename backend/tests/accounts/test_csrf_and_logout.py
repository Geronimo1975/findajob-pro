"""``GET /auth/csrf/`` and ``POST /auth/logout/`` — spec §§3, 8."""

from __future__ import annotations

from django.conf import settings
from django.test import Client

from apps.accounts.models import User
from tests.accounts.support import CSRF_URL, LOGOUT_URL, AuthTestCase, csrf_client


class CsrfEndpointTests(AuthTestCase):
    def test_returns_ok_and_sets_the_cookie(self):
        response = self.client.get(CSRF_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"detail": "ok"})
        self.assertIn("csrftoken", response.cookies)
        self.assertTrue(response.cookies["csrftoken"].value)

    def test_the_cookie_is_readable_by_javascript(self):
        # web/api.js reads it and echoes it back in X-CSRFToken (spec §8).
        # HttpOnly here would silently break every anonymous POST.
        self.assertFalse(settings.CSRF_COOKIE_HTTPONLY)
        response = self.client.get(CSRF_URL)
        self.assertFalse(response.cookies["csrftoken"].get("httponly"))

    def test_it_needs_no_session(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(CSRF_URL).status_code, 200)


class LogoutTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email="george@example.de", is_verified=True)

    def test_an_authenticated_post_returns_204_and_clears_the_session(self):
        self.client.force_login(self.user)
        response = self.client.post(LOGOUT_URL)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_an_anonymous_post_is_401_not_403(self):
        # web/app.html boots against /api/v1/me/ and branches on 401 (spec §7);
        # the same distinction has to hold everywhere or the shell mis-routes.
        response = self.client.post(LOGOUT_URL)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")

    def test_get_is_not_allowed(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(LOGOUT_URL).status_code, 405)

    def test_csrf_is_required(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        response = client.post(LOGOUT_URL)
        self.assertEqual(response.status_code, 403)

    def test_a_post_with_a_token_succeeds(self):
        client, token = csrf_client()
        client.force_login(self.user)
        response = client.post(LOGOUT_URL, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(response.status_code, 204)


class EndpointInventoryTests(AuthTestCase):
    """Spec §3 is the *complete, exclusive* list."""

    def test_there_is_no_auth_me_or_auth_session(self):
        for path in ("/api/v1/auth/me/", "/api/v1/auth/session/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)

    def test_the_four_auth_routes_resolve(self):
        from django.urls import reverse

        for name in ("csrf", "request-code", "verify-code", "logout"):
            with self.subTest(name=name):
                self.assertTrue(reverse(f"accounts:{name}").startswith("/api/v1/auth/"))

    def test_accounts_registers_exactly_four_routes(self):
        from apps.accounts import urls as accounts_urls

        self.assertEqual(len(accounts_urls.urlpatterns), 4)
