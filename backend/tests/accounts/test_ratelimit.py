"""Rate-limit primitives (spec §5), including how they behave with no Redis."""

from __future__ import annotations

import pathlib
import time
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase

from apps.accounts import ratelimit


class BrokenCache:
    """Every operation raises, like a Redis that has gone away."""

    class Boom(RuntimeError):
        pass

    def __getattr__(self, name):
        def explode(*args, **kwargs):
            raise self.Boom(f"cache.{name} unavailable")

        return explode


class NoCacheTtlTests(SimpleTestCase):
    """``cache.ttl()`` does not exist in Django's cache API (spec §5)."""

    def test_module_never_calls_cache_ttl(self):
        """Parsed, not grepped: the module docstring explains why it must not."""
        import ast

        tree = ast.parse(
            pathlib.Path(ratelimit.__file__).read_text(encoding="utf-8")
        )
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("ttl", called)

    def test_django_cache_really_has_no_ttl(self):
        # Guards the assumption the whole design rests on: if a future Django
        # grew `ttl()`, this test tells us the workaround can be simplified.
        self.assertFalse(hasattr(cache, "ttl"))


class SubnetTests(SimpleTestCase):
    def test_ipv4_is_bucketed_by_24(self):
        self.assertEqual(ratelimit.subnet_of("203.0.113.4"), "203.0.113.0/24")
        self.assertEqual(
            ratelimit.subnet_of("203.0.113.4"), ratelimit.subnet_of("203.0.113.250")
        )
        self.assertNotEqual(
            ratelimit.subnet_of("203.0.113.4"), ratelimit.subnet_of("203.0.114.4")
        )

    def test_ipv6_is_bucketed_by_64(self):
        # The VPS itself egresses over IPv6 (spec §6.1), so this is not academic.
        self.assertEqual(
            ratelimit.subnet_of("2a02:2479:84:3500::1"),
            ratelimit.subnet_of("2a02:2479:84:3500:dead:beef::9"),
        )
        self.assertNotEqual(
            ratelimit.subnet_of("2a02:2479:84:3500::1"),
            ratelimit.subnet_of("2a02:2479:84:3501::1"),
        )

    def test_missing_or_junk_addresses_share_one_bucket(self):
        self.assertEqual(ratelimit.subnet_of(None), "unknown")
        self.assertEqual(ratelimit.subnet_of("not-an-ip"), "unknown")


class KeyTests(SimpleTestCase):
    def test_identifiers_are_hashed_not_embedded(self):
        key = ratelimit.cooldown_key("george@example.de")
        self.assertTrue(key.startswith("otp:cd:"))
        self.assertNotIn("george", key)
        self.assertNotIn("@", key)

    def test_keys_are_stable_and_distinct(self):
        self.assertEqual(
            ratelimit.per_email_key("a@b.de"), ratelimit.per_email_key("a@b.de")
        )
        self.assertNotEqual(
            ratelimit.per_email_key("a@b.de"), ratelimit.per_email_key("c@d.de")
        )

    def test_keys_stay_well_under_the_250_character_limit(self):
        long_address = ("x" * 240) + "@example.de"
        for key in (
            ratelimit.cooldown_key(long_address),
            ratelimit.per_email_key(long_address),
            ratelimit.verify_failure_key(long_address, "203.0.113.4"),
        ):
            self.assertLess(len(key), 120)


class BudgetTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_cooldown_is_claimed_once(self):
        self.assertTrue(ratelimit.claim_resend_cooldown("a@b.de").allowed)
        second = ratelimit.claim_resend_cooldown("a@b.de")
        self.assertFalse(second.allowed)
        self.assertGreater(second.retry_after, 0)
        self.assertLessEqual(second.retry_after, ratelimit.RESEND_COOLDOWN_SECONDS)

    def test_cooldown_can_be_released(self):
        ratelimit.claim_resend_cooldown("a@b.de")
        ratelimit.release_resend_cooldown("a@b.de")
        self.assertTrue(ratelimit.claim_resend_cooldown("a@b.de").allowed)

    def test_per_email_budget_allows_exactly_the_limit(self):
        for attempt in range(ratelimit.PER_EMAIL_LIMIT):
            decision = ratelimit.check_request_budgets("a@b.de", "203.0.113.4")
            self.assertTrue(decision.allowed, f"rejected on attempt {attempt + 1}")
        blocked = ratelimit.check_request_budgets("a@b.de", "203.0.113.4")
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.budget, "per_email")

    def test_retry_after_is_never_zero(self):
        ratelimit.claim_resend_cooldown("a@b.de")
        # Fake an already-elapsed deadline: the counter is still live but the
        # sibling deadline key says the window is over.
        cache.set(ratelimit.cooldown_key("a@b.de") + ":exp", time.time() - 5, 60)
        decision = ratelimit.claim_resend_cooldown("a@b.de")
        self.assertFalse(decision.allowed)
        self.assertGreaterEqual(decision.retry_after, 1)

    def test_missing_deadline_key_falls_back_to_the_window(self):
        ratelimit.claim_resend_cooldown("a@b.de")
        cache.delete(ratelimit.cooldown_key("a@b.de") + ":exp")
        decision = ratelimit.claim_resend_cooldown("a@b.de")
        self.assertEqual(decision.retry_after, ratelimit.RESEND_COOLDOWN_SECONDS)

    def test_verify_budget_blocks_after_ten_failures(self):
        for _ in range(ratelimit.VERIFY_FAILURE_LIMIT):
            self.assertTrue(ratelimit.check_verify_budget("a@b.de", "203.0.113.4").allowed)
            ratelimit.record_verify_failure("a@b.de", "203.0.113.4")
        blocked = ratelimit.check_verify_budget("a@b.de", "203.0.113.4")
        self.assertFalse(blocked.allowed)
        self.assertGreater(blocked.retry_after, 0)

    def test_verify_budget_is_scoped_to_the_subnet(self):
        for _ in range(ratelimit.VERIFY_FAILURE_LIMIT):
            ratelimit.record_verify_failure("a@b.de", "203.0.113.4")
        self.assertFalse(ratelimit.check_verify_budget("a@b.de", "203.0.113.9").allowed)
        # A different /24 is a different budget: there is no per-email lockout,
        # so an attacker elsewhere cannot deny the mailbox owner their login.
        self.assertTrue(ratelimit.check_verify_budget("a@b.de", "198.51.100.4").allowed)

    def test_successful_login_clears_the_guessing_budget(self):
        for _ in range(ratelimit.VERIFY_FAILURE_LIMIT):
            ratelimit.record_verify_failure("a@b.de", "203.0.113.4")
        ratelimit.reset_verify_failures("a@b.de", "203.0.113.4")
        self.assertTrue(ratelimit.check_verify_budget("a@b.de", "203.0.113.4").allowed)


class ProgressiveBackoffTests(SimpleTestCase):
    """Past a soft budget the first rejections get a short, escalating wait.

    The budgets used to be binary: one request under the limit was free, the
    next was told to wait out the rest of a one-hour window. That is a wall for
    an honest user who needed one more code (German greylisting, a typo, a
    second device) and barely an inconvenience for an attacker, who was never
    going to sit through it either way.
    """

    IP = "203.0.113.4"

    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def _exhaust_per_email(self, email: str = "a@b.de") -> None:
        for _ in range(ratelimit.PER_EMAIL_LIMIT):
            ratelimit.check_request_budgets(email, self.IP)

    def test_the_first_rejections_climb_the_ladder(self):
        self._exhaust_per_email()
        for expected in ratelimit.BACKOFF_LADDER:
            decision = ratelimit.check_request_budgets("a@b.de", self.IP)
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.budget, "per_email")
            self.assertEqual(decision.retry_after, expected)

    def test_after_the_ladder_the_full_window_returns(self):
        self._exhaust_per_email()
        for _ in ratelimit.BACKOFF_LADDER:
            ratelimit.check_request_budgets("a@b.de", self.IP)
        decision = ratelimit.check_request_budgets("a@b.de", self.IP)
        self.assertFalse(decision.allowed)
        self.assertGreater(decision.retry_after, max(ratelimit.BACKOFF_LADDER))

    def test_the_ladder_never_quotes_longer_than_the_window(self):
        # The whole point of min(rung, remaining): backoff may only ever be
        # more permissive than the behaviour it replaced, never less.
        self._exhaust_per_email()
        decision = ratelimit.check_request_budgets("a@b.de", self.IP)
        self.assertLessEqual(decision.retry_after, ratelimit.PER_EMAIL_WINDOW)

    def test_a_budget_without_backoff_stays_hard(self):
        # The global budget protects the sending domain's reputation and the
        # provider bill; softening it would let a flood keep trickling through.
        # `_consume` defaults to backoff=False, so this is that default.
        for _ in range(3):
            ratelimit._consume("test:hard", limit=1, window=600, budget="global")
        decision = ratelimit._consume("test:hard", limit=1, window=600, budget="global")
        self.assertFalse(decision.allowed)
        self.assertGreater(decision.retry_after, max(ratelimit.BACKOFF_LADDER))

    def test_the_verify_budget_stays_hard(self):
        # Spec §5: this is the online-guessing oracle defence. No grace.
        for _ in range(ratelimit.VERIFY_FAILURE_LIMIT):
            ratelimit.record_verify_failure("a@b.de", self.IP)
        decision = ratelimit.check_verify_budget("a@b.de", self.IP)
        self.assertFalse(decision.allowed)
        self.assertGreater(decision.retry_after, max(ratelimit.BACKOFF_LADDER))


class DegradedCacheTests(SimpleTestCase):
    """Redis down must degrade, not 500 (spec §5)."""

    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(ratelimit, "cache", BrokenCache())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_cooldown_fails_open_and_reports_degraded(self):
        decision = ratelimit.claim_resend_cooldown("a@b.de")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.degraded)

    def test_request_budgets_fail_open_and_report_degraded(self):
        decision = ratelimit.check_request_budgets("a@b.de", "203.0.113.4")
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.degraded)

    def test_verify_budget_fails_closed(self):
        decision = ratelimit.check_verify_budget("a@b.de", "203.0.113.4")
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.degraded)
        self.assertGreaterEqual(decision.retry_after, 1)

    def test_release_and_reset_never_raise(self):
        ratelimit.release_resend_cooldown("a@b.de")
        ratelimit.reset_verify_failures("a@b.de", "203.0.113.4")
