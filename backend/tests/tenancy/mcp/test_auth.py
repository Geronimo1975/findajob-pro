"""Bearer authentication on ``/mcp/`` (spec §4)."""
from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase, override_settings
from django.utils import timezone

from apps.tenancy.models import Membership
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user
from tests.tenancy.mcp.support import MCP_URL, make_token, rpc


class McpAuthTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.membership = bootstrap_workspace(self.user)
        self.org = self.membership.org
        self.raw, self.token = make_token(self.user, self.org)

    def _assert_401(self, response, code):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], "Bearer")
        self.assertEqual(response.json()["error"]["code"], code)

    def test_missing_header(self):
        self._assert_401(rpc(self.client, None, "ping"), "token_missing")

    def test_garbage_token(self):
        self._assert_401(rpc(self.client, "nope", "ping"), "token_invalid")

    def test_wrong_tail_same_prefix_is_401(self):
        forged = self.raw[:12] + "x" * 32
        self._assert_401(rpc(self.client, forged, "ping"), "token_invalid")

    def test_expired(self):
        self.token.expires_at = timezone.now() - timedelta(seconds=1)
        self.token.save()
        self._assert_401(rpc(self.client, self.raw, "ping"), "token_expired")

    def test_revoked(self):
        self.token.revoked_at = timezone.now()
        self.token.save()
        self._assert_401(rpc(self.client, self.raw, "ping"), "token_revoked")

    def test_membership_removed_kills_token(self):
        Membership.all_objects.filter(pk=self.membership.pk).delete()
        self._assert_401(rpc(self.client, self.raw, "ping"), "token_invalid")

    def test_session_cookie_alone_is_rejected(self):
        self.client.force_login(self.user)
        self._assert_401(rpc(self.client, None, "ping"), "token_missing")

    def test_valid_token_pings_and_touches_last_used(self):
        r = rpc(self.client, self.raw, "ping")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.token.refresh_from_db()
        self.assertIsNotNone(self.token.last_used_at)

    @override_settings(MCP_SERVER_ENABLED=False)
    def test_feature_flag_off_is_404_envelope(self):
        r = rpc(self.client, self.raw, "ping")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r.json()["error"]["code"], "not_found")

    def test_get_is_405_and_delete_is_204(self):
        self.assertEqual(self.client.get(MCP_URL).status_code, 405)
        r = self.client.delete(MCP_URL, HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(r.status_code, 204)

    def test_csrf_enforced_client_bearer_passes_without_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        r = rpc(client, self.raw, "ping")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"], {})

    def test_csrf_enforced_client_session_alone_is_401(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.user)
        self._assert_401(rpc(client, None, "ping"), "token_missing")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_last_used_ip_honours_trusted_proxy_count(self):
        r = self.client.post(
            MCP_URL, data='{"jsonrpc": "2.0", "id": 1, "method": "ping"}',
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.raw}",
            HTTP_X_FORWARDED_FOR="203.0.113.9", REMOTE_ADDR="10.0.0.2",
        )
        self.assertEqual(r.status_code, 200)
        self.token.refresh_from_db()
        self.assertEqual(self.token.last_used_ip, "203.0.113.9")
