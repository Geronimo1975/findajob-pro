from __future__ import annotations

from unittest import mock

from django.core.cache import cache
from django.test import TestCase

from apps.tenancy.mcp.ratelimit import PER_MINUTE, McpRateLimited, check_budget
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user
from tests.tenancy.mcp.support import make_token, rpc


class BudgetTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_allows_up_to_limit_then_rejects(self):
        for _ in range(PER_MINUTE):
            check_budget(1)
        with self.assertRaises(McpRateLimited) as ctx:
            check_budget(1)
        self.assertGreaterEqual(ctx.exception.retry_after, 1)

    def test_tokens_are_independent(self):
        for _ in range(PER_MINUTE):
            check_budget(1)
        check_budget(2)

    def test_cache_failure_fails_closed(self):
        with mock.patch("apps.tenancy.mcp.ratelimit.cache.add", side_effect=RuntimeError("redis down")):
            with self.assertRaises(McpRateLimited):
                check_budget(3)

    def test_cache_failure_while_reading_deadline_fails_closed(self):
        for _ in range(PER_MINUTE):
            check_budget(4)
        with mock.patch("apps.tenancy.mcp.ratelimit.cache.get", side_effect=RuntimeError("redis down")):
            with self.assertRaises(McpRateLimited):
                check_budget(4)


class BudgetHttpTests(TestCase):
    def test_429_envelope_with_retry_after(self):
        cache.clear()
        user = make_user()
        org = bootstrap_workspace(user).org
        raw, token = make_token(user, org)
        with mock.patch("apps.tenancy.mcp.views.check_budget", side_effect=McpRateLimited(17)):
            r = rpc(self.client, raw, "ping")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r.json()["error"]["code"], "rate_limited")
        self.assertEqual(r["Retry-After"], "17")
