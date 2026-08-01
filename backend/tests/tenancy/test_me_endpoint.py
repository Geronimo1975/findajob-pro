"""``GET /api/v1/me/`` — the single session endpoint (implementation spec §3).

The shape below is pinned. ``usage`` and ``flags`` may be *added* later by
``entitlements-quotas``; nothing may be removed or renamed, because
``web/app.html`` boots against exactly these keys.
"""

from __future__ import annotations

from django.test import TestCase
from django.urls import reverse

from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user

ME_URL = "/api/v1/me/"


class MeEndpointRoutingTests(TestCase):
    def test_url_is_exactly_api_v1_me(self):
        self.assertEqual(reverse("tenancy:me"), ME_URL)

    def test_there_is_no_auth_me_or_auth_session_route(self):
        """Spec §3: 'There is no /auth/me/ and no /auth/session/.'"""
        for path in ("/api/v1/auth/me/", "/api/v1/auth/session/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 404)


class MeEndpointAnonymousTests(TestCase):
    def test_anonymous_request_is_401_not_403(self):
        """app.html redirects to /auth.html on 401 — 403 would strand the user."""
        response = self.client.get(ME_URL)
        self.assertEqual(response.status_code, 401)

    def test_anonymous_response_uses_the_shared_error_envelope(self):
        response = self.client.get(ME_URL)
        body = response.json()
        self.assertEqual(set(body), {"error"})
        self.assertTrue({"code", "detail"} <= set(body["error"]))
        self.assertEqual(body["error"]["code"], "not_authenticated")
        self.assertIsInstance(body["error"]["detail"], str)

    def test_anonymous_response_leaks_nothing(self):
        payload = self.client.get(ME_URL).content.decode()
        for leaked in ("Traceback", "sessionid", "csrftoken"):
            self.assertNotIn(leaked, payload)


class MeEndpointAuthenticatedTests(TestCase):
    def setUp(self):
        self.user = make_user("andrei@popescu.dev")
        self.membership = bootstrap_workspace(self.user)
        self.org = self.membership.org
        self.client.force_login(self.user)

    def test_returns_the_pinned_top_level_shape(self):
        response = self.client.get(ME_URL)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(
            set(body),
            {"user", "org", "membership_role", "memberships", "lang"},
        )

    def test_user_object_shape(self):
        body = self.client.get(ME_URL).json()

        self.assertEqual(set(body["user"]), {"id", "email", "is_verified", "lang"})
        self.assertEqual(body["user"]["id"], self.user.pk)
        self.assertEqual(body["user"]["email"], "andrei@popescu.dev")
        self.assertIsInstance(body["user"]["is_verified"], bool)
        self.assertIn(body["user"]["lang"], {"en", "de", "ro"})

    def test_org_object_shape(self):
        body = self.client.get(ME_URL).json()

        self.assertEqual(set(body["org"]), {"id", "name", "slug"})
        self.assertEqual(body["org"]["id"], self.org.pk)
        self.assertEqual(body["org"]["name"], self.org.name)
        self.assertEqual(body["org"]["slug"], self.org.slug)

    def test_membership_role_and_memberships(self):
        body = self.client.get(ME_URL).json()

        self.assertEqual(body["membership_role"], "owner")
        self.assertEqual(len(body["memberships"]), 1)
        entry = body["memberships"][0]
        self.assertEqual(set(entry), {"id", "role", "org"})
        self.assertEqual(entry["id"], self.membership.pk)
        self.assertEqual(entry["role"], "owner")
        self.assertEqual(entry["org"]["id"], self.org.pk)

    def test_lang_is_a_supported_code(self):
        body = self.client.get(ME_URL).json()
        self.assertIn(body["lang"], {"en", "de", "ro"})
        self.assertEqual(body["lang"], body["user"]["lang"])

    def test_response_is_json_only(self):
        response = self.client.get(ME_URL)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_user_without_a_workspace_still_gets_a_well_formed_payload(self):
        loner = make_user("noworkspace@example.de")
        self.client.force_login(loner)

        body = self.client.get(ME_URL).json()

        self.assertIsNone(body["org"])
        self.assertIsNone(body["membership_role"])
        self.assertEqual(body["memberships"], [])
        self.assertEqual(body["user"]["id"], loner.pk)

    def test_only_get_is_allowed(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                response = getattr(self.client, method)(ME_URL)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(
                    response.json()["error"]["code"], "method_not_allowed"
                )
