from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.tenancy.mcp.models import McpToken
from apps.tenancy.models import AuditLog
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user

URL = "/api/v1/mcp-tokens/"
BODY = {"name": "Claude Desktop", "scopes": ["jobs:read", "applications:read"]}


class AnonymousTests(TestCase):
    def test_401_envelope(self):
        r = APIClient().get(URL)
        self.assertEqual(r.status_code, 401)
        self.assertIn("error", r.json())


class TokenApiTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.client = APIClient()
        self.client.force_login(self.user)

    def test_create_returns_plaintext_once_and_stores_hash_only(self):
        r = self.client.post(URL, BODY, format="json")
        self.assertEqual(r.status_code, 201)
        data = r.json()
        self.assertTrue(data["token"].startswith("fjp_"))
        self.assertEqual(len(data["token"]), 44)
        self.assertEqual(data["prefix"], data["token"][:12])
        self.assertEqual(data["scopes"], BODY["scopes"])
        self.assertIsNotNone(data["expires_at"])
        row = McpToken.all_objects.get()
        self.assertNotEqual(row.token_hash, data["token"])
        listed = self.client.get(URL).json()
        self.assertEqual(len(listed), 1)
        self.assertNotIn("token", listed[0])
        self.assertNotIn("token_hash", listed[0])
        self.assertEqual(listed[0]["status"], "active")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_audit_ip_is_proxy_aware(self):
        r = self.client.post(
            URL, BODY, format="json",
            HTTP_X_FORWARDED_FOR="203.0.113.7", REMOTE_ADDR="10.0.0.2",
        )
        self.assertEqual(r.status_code, 201)
        row = AuditLog.objects.unscoped().get(action="mcp_token.created")
        self.assertEqual(row.ip, "203.0.113.7")

    def test_unknown_scope_is_400(self):
        r = self.client.post(URL, {"name": "x", "scopes": ["admin:all"]}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["code"], "invalid")

    def test_empty_scopes_is_400(self):
        r = self.client.post(URL, {"name": "x", "scopes": []}, format="json")
        self.assertEqual(r.status_code, 400)

    def test_expires_in_days_bounds(self):
        self.assertEqual(self.client.post(URL, dict(BODY, expires_in_days=0), format="json").status_code, 400)
        self.assertEqual(self.client.post(URL, dict(BODY, expires_in_days=366), format="json").status_code, 400)
        self.assertEqual(self.client.post(URL, dict(BODY, expires_in_days=30), format="json").status_code, 201)

    def test_revoke_is_soft_and_audited(self):
        token_id = self.client.post(URL, BODY, format="json").json()["id"]
        r = self.client.delete(f"{URL}{token_id}/")
        self.assertEqual(r.status_code, 204)
        row = McpToken.all_objects.get(pk=token_id)
        self.assertIsNotNone(row.revoked_at)
        self.assertEqual(self.client.get(URL).json()[0]["status"], "revoked")
        actions = set(AuditLog.objects.for_org(self.org).values_list("action", flat=True))
        self.assertEqual(actions, {"mcp_token.created", "mcp_token.revoked"})

    def test_cannot_revoke_another_members_token(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        foreign = McpToken.objects.create(org=other_org, user=other, name="f",
                                          prefix="fjp_aaaaaaaa", token_hash="0" * 64,
                                          scopes=["jobs:read"])
        r = self.client.delete(f"{URL}{foreign.pk}/")
        self.assertEqual(r.status_code, 404)
        self.assertIsNone(McpToken.all_objects.get(pk=foreign.pk).revoked_at)
