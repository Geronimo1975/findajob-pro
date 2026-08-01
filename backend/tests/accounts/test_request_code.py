"""``POST /api/v1/auth/request-code/`` — spec §§3, 4, 5, 6, 8."""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import Client
from django.utils import timezone

from apps.accounts import mailer, ratelimit, services
from apps.accounts.models import (
    MAX_LIVE_OTPS,
    OTP_RESEND_COOLDOWN_SECONDS,
    OTP_TTL_SECONDS,
    EmailOTP,
    SuppressedAddress,
    User,
)
from tests.accounts.support import (
    REQUEST_URL,
    AuthTestCase,
    csrf_client,
    extract_code,
)


class ResponseShapeTests(AuthTestCase):
    def test_returns_202_with_the_pinned_body(self):
        response = self.request_code("george@example.de")
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(
            sorted(body), ["expires_in", "masked_email", "resend_in"]
        )
        self.assertEqual(body["expires_in"], OTP_TTL_SECONDS)
        self.assertEqual(body["resend_in"], OTP_RESEND_COOLDOWN_SECONDS)

    def test_masked_email_keeps_the_domain_and_hides_the_local_part(self):
        body = self.request_code("george@example.de").json()
        self.assertTrue(body["masked_email"].endswith("@example.de"))
        self.assertNotIn("george", body["masked_email"])

    def test_writes_one_row_and_sends_one_email(self):
        self.request_code("george@example.de")
        self.assertEqual(EmailOTP.objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["george@example.de"])

    def test_address_is_canonicalised_before_anything_else(self):
        self.request_code("  George@Example.DE ")
        otp = EmailOTP.objects.get()
        self.assertEqual(otp.email, "george@example.de")

    def test_expiry_is_ten_minutes(self):
        self.request_code("george@example.de")
        otp = EmailOTP.objects.get()
        delta = otp.expires_at - otp.created_at
        self.assertAlmostEqual(delta.total_seconds(), OTP_TTL_SECONDS, delta=2)


class EnumerationParityTests(AuthTestCase):
    """Spec §6: identical status **and** shape for known vs unknown addresses."""

    def test_known_and_unknown_addresses_are_indistinguishable(self):
        User.objects.create_user(email="known@example.de")

        known = self.request_code("known@example.de")
        unknown = self.request_code("unknown@example.de")

        self.assertEqual(known.status_code, unknown.status_code)
        self.assertEqual(known.status_code, 202)

        known_body = known.json()
        unknown_body = unknown.json()
        self.assertEqual(sorted(known_body), sorted(unknown_body))
        self.assertEqual(known_body["expires_in"], unknown_body["expires_in"])
        self.assertEqual(known_body["resend_in"], unknown_body["resend_in"])
        # Only the mask differs, and only because the address does.
        # chr(0x2022) rather than a literal bullet: see the note in
        # apps/accounts/serializers.py about non-ASCII literals in source.
        self.assertEqual(
            known_body["masked_email"], "k" + chr(0x2022) * 3 + "@example.de"
        )

    def test_both_addresses_are_actually_mailed(self):
        User.objects.create_user(email="known@example.de")
        self.request_code("known@example.de")
        self.request_code("unknown@example.de")
        self.assertEqual(len(mail.outbox), 2)

    def test_the_request_path_never_queries_the_user_table(self):
        # The strongest available statement of "branch-free": if User is never
        # read, no amount of timing analysis can reveal whether it has a row.
        User.objects.create_user(email="known@example.de")
        with mock.patch.object(
            User.objects, "filter", side_effect=AssertionError("User was queried")
        ):
            response = self.request_code("known@example.de")
        self.assertEqual(response.status_code, 202)


class SecrecyTests(AuthTestCase):
    def test_the_plaintext_code_is_never_stored(self):
        self.request_code("george@example.de")
        code = extract_code()
        otp = EmailOTP.objects.get()
        stored = json.dumps(
            {
                field.name: str(getattr(otp, field.name))
                for field in EmailOTP._meta.fields
            }
        )
        self.assertNotIn(code, stored)

    def test_the_plaintext_code_is_never_returned(self):
        response = self.request_code("george@example.de")
        code = extract_code()
        self.assertNotIn(code, response.content.decode())

    def test_the_plaintext_code_is_never_logged(self):
        with self.assertLogs("findajob", level="DEBUG") as captured:
            self.request_code("george@example.de")
            # assertLogs fails when nothing is logged; give it something.
            import logging

            logging.getLogger("findajob").info("marker")
        code = extract_code()
        self.assertNotIn(code, "\n".join(captured.output))

    def test_the_stored_hash_matches_the_emailed_code(self):
        self.request_code("george@example.de")
        code = extract_code()
        self.assertTrue(EmailOTP.objects.get().matches(code))


class LiveCodeCapTests(AuthTestCase):
    """Spec §4: up to three coexist, and a resend never supersedes."""

    def test_three_codes_stay_live(self):
        codes = [services.issue_code(email="a@b.de")[1] for _ in range(3)]
        live = list(EmailOTP.objects.live(email="a@b.de"))
        self.assertEqual(len(live), MAX_LIVE_OTPS)
        for code in codes:
            self.assertTrue(any(row.matches(code) for row in live))

    def test_a_resend_does_not_invalidate_the_previous_code(self):
        _, first = services.issue_code(email="a@b.de")
        services.issue_code(email="a@b.de")
        live = list(EmailOTP.objects.live(email="a@b.de"))
        self.assertTrue(any(row.matches(first) for row in live))

    def test_a_fourth_code_retires_the_oldest_only(self):
        _, oldest = services.issue_code(email="a@b.de")
        _, second = services.issue_code(email="a@b.de")
        _, third = services.issue_code(email="a@b.de")
        _, fourth = services.issue_code(email="a@b.de")

        live = list(EmailOTP.objects.live(email="a@b.de"))
        self.assertEqual(len(live), MAX_LIVE_OTPS)
        self.assertFalse(any(row.matches(oldest) for row in live))
        for code in (second, third, fourth):
            self.assertTrue(any(row.matches(code) for row in live))

    def test_retiring_uses_expiry_not_consumption(self):
        # consumed_at means "this code logged somebody in". Overloading it would
        # destroy the audit trail.
        for _ in range(4):
            services.issue_code(email="a@b.de")
        self.assertFalse(EmailOTP.objects.filter(consumed_at__isnull=False).exists())


class RateLimitTests(AuthTestCase):
    def test_a_second_request_inside_60s_is_429_with_retry_after(self):
        self.request_code("george@example.de")
        response = self.request_code("george@example.de")
        self.assertEqual(response.status_code, 429)
        error = response.json()["error"]
        self.assertEqual(error["code"], "rate_limited")
        self.assertGreaterEqual(error["retry_after"], 1)
        self.assertEqual(response["Retry-After"], str(error["retry_after"]))

    def test_retry_after_is_never_an_attribute_error(self):
        # The whole point of spec §12's wording: DRF's Throttled carries `wait`,
        # ours carries `retry_after`, and the envelope must survive both.
        self.request_code("george@example.de")
        response = self.request_code("george@example.de")
        self.assertIn("retry_after", response.json()["error"])
        self.assertIsInstance(response.json()["error"]["retry_after"], int)

    def test_a_second_request_for_a_different_address_is_allowed(self):
        self.assertEqual(self.request_code("a@example.de").status_code, 202)
        self.assertEqual(self.request_code("b@example.de").status_code, 202)

    def test_per_email_hourly_budget_is_enforced(self):
        # Bypass only the 60 s cooldown; the hourly counter is the thing here.
        with mock.patch.object(
            services.ratelimit, "claim_resend_cooldown", return_value=ratelimit.ALLOWED
        ):
            for _ in range(ratelimit.PER_EMAIL_LIMIT):
                self.assertEqual(self.request_code("a@example.de").status_code, 202)
            blocked = self.request_code("a@example.de")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"]["code"], "rate_limited")

    def test_the_response_never_names_which_budget_rejected(self):
        self.request_code("george@example.de")
        body = self.request_code("george@example.de").json()
        self.assertNotIn("budget", body["error"])
        for word in ("cooldown", "per_email", "per_ip", "global"):
            self.assertNotIn(word, json.dumps(body))


class DegradedCacheTests(AuthTestCase):
    """Spec §5: request-code fails OPEN, with a database fallback."""

    def setUp(self):
        super().setUp()
        from tests.accounts.test_ratelimit import BrokenCache

        patcher = mock.patch.object(ratelimit, "cache", BrokenCache())
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_dead_cache_does_not_500(self):
        response = self.request_code("george@example.de")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(mail.outbox), 1)

    def test_the_database_still_enforces_the_60s_cooldown(self):
        self.assertEqual(self.request_code("george@example.de").status_code, 202)
        second = self.request_code("george@example.de")
        self.assertEqual(second.status_code, 429)
        self.assertGreaterEqual(second.json()["error"]["retry_after"], 1)

    def test_the_database_fallback_expires(self):
        self.request_code("george@example.de")
        old = timezone.now() - timedelta(seconds=OTP_RESEND_COOLDOWN_SECONDS + 5)
        EmailOTP.objects.update(created_at=old)
        self.assertEqual(self.request_code("george@example.de").status_code, 202)


class DeliveryFailureTests(AuthTestCase):
    def test_a_provider_error_is_503_delivery_unavailable(self):
        with mock.patch.object(
            mailer, "get_connection", side_effect=OSError("connection refused")
        ):
            response = self.request_code("george@example.de")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "delivery_unavailable")

    def test_a_backend_that_sends_zero_messages_is_also_503(self):
        # The silent failure spec §1 exists to prevent: a green 202 and nothing
        # in the user's inbox.
        with mock.patch(
            "django.core.mail.EmailMultiAlternatives.send", return_value=0
        ):
            response = self.request_code("george@example.de")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "delivery_unavailable")

    def test_a_failed_send_leaves_no_orphan_row(self):
        with mock.patch.object(mailer, "get_connection", side_effect=OSError):
            self.request_code("george@example.de")
        self.assertEqual(EmailOTP.objects.count(), 0)

    def test_a_failed_send_releases_the_cooldown(self):
        with mock.patch.object(mailer, "get_connection", side_effect=OSError):
            self.request_code("george@example.de")
        # The user was never given a code, so they must not be made to wait.
        self.assertEqual(self.request_code("george@example.de").status_code, 202)


class SuppressionTests(AuthTestCase):
    def test_a_suppressed_address_still_gets_202(self):
        SuppressedAddress.suppress("george@example.de")
        response = self.request_code("george@example.de")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(
            sorted(response.json()), ["expires_in", "masked_email", "resend_in"]
        )

    def test_a_suppressed_address_is_not_mailed(self):
        SuppressedAddress.suppress("george@example.de")
        self.request_code("george@example.de")
        self.assertEqual(len(mail.outbox), 0)


class ValidationTests(AuthTestCase):
    def test_a_malformed_address_is_400_invalid_email(self):
        for bad in ("", "not-an-email", "a@", "@b.de", "a b@c.de"):
            with self.subTest(bad=bad):
                response = self.request_code(bad)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_email")

    def test_a_missing_body_is_400(self):
        response = self.client.post(REQUEST_URL, "{}", content_type="application/json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_email")

    def test_an_unknown_language_falls_back_instead_of_failing(self):
        response = self.post_json(
            REQUEST_URL, {"email": "a@b.de", "lang": "klingon"}
        )
        self.assertEqual(response.status_code, 202)

    def test_a_regional_language_tag_is_accepted(self):
        response = self.post_json(REQUEST_URL, {"email": "a@b.de", "lang": "de-AT"})
        self.assertEqual(response.status_code, 202)
        self.assertIn("Anmeldecode", mail.outbox[0].subject)

    def test_the_email_is_written_in_the_requested_language(self):
        for lang, marker in (("en", "login code"), ("de", "Anmeldecode"), ("ro", "cod")):
            with self.subTest(lang=lang):
                mail.outbox.clear()
                from django.core.cache import cache as django_cache

                django_cache.clear()
                self.request_code(f"{lang}@example.de", lang=lang)
                self.assertIn(marker.lower(), mail.outbox[0].subject.lower())


class CsrfTests(AuthTestCase):
    """Spec §8: without the explicit decorator, login CSRF is live."""

    def test_a_post_without_a_token_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            REQUEST_URL,
            {"email": "a@b.de"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")
        self.assertEqual(EmailOTP.objects.count(), 0)

    def test_a_post_with_a_token_succeeds(self):
        client, token = csrf_client()
        response = client.post(
            REQUEST_URL,
            {"email": "a@b.de"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 202)

    def test_a_wrong_token_is_rejected(self):
        client, _token = csrf_client()
        response = client.post(
            REQUEST_URL,
            {"email": "a@b.de"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN="x" * 64,
        )
        self.assertEqual(response.status_code, 403)


class ClientIpTests(AuthTestCase):
    def test_the_recorded_ip_is_remote_addr_by_default(self):
        self.request_code("a@b.de", REMOTE_ADDR="203.0.113.9")
        self.assertEqual(EmailOTP.objects.get().request_ip, "203.0.113.9")

    def test_a_forged_forwarded_header_is_ignored_when_no_proxy_is_trusted(self):
        self.request_code(
            "a@b.de",
            REMOTE_ADDR="203.0.113.9",
            HTTP_X_FORWARDED_FOR="1.2.3.4",
        )
        self.assertEqual(EmailOTP.objects.get().request_ip, "203.0.113.9")

    def test_with_one_trusted_proxy_the_appended_entry_wins(self):
        with self.settings(TRUSTED_PROXY_COUNT=1):
            self.request_code(
                "a@b.de",
                REMOTE_ADDR="10.0.0.2",
                # The visitor forged the first entry; Traefik appended the real one.
                HTTP_X_FORWARDED_FOR="1.2.3.4, 198.51.100.7",
            )
        self.assertEqual(EmailOTP.objects.get().request_ip, "198.51.100.7")
