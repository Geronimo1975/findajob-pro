"""``POST /api/v1/auth/verify-code/`` — spec §§3, 4, 5, 8, 10."""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.test import Client
from django.utils import timezone

from apps.accounts import ratelimit, services
from apps.accounts.models import (
    MAX_OTP_ATTEMPTS,
    EmailOTP,
    User,
)
from apps.tenancy.models import AuditLog, Membership, Organization, Role
from tests.accounts.support import (
    ME_URL,
    TERMS_VERSION,
    VERIFY_URL,
    AuthTestCase,
    csrf_client,
    seed_legal_documents,
)

EMAIL = "george@example.de"


class HappyPathTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        seed_legal_documents()

    def test_a_valid_code_creates_the_account_and_a_session(self):
        code = self.request_and_read_code(EMAIL)
        response = self.verify(EMAIL, code, accepts_terms=True)

        self.assertEqual(response.status_code, 200)
        user = User.objects.get(email=EMAIL)
        self.assertTrue(user.is_verified)
        self.assertIn("sessionid", response.cookies)
        self.assertEqual(int(self.client.session["_auth_user_id"]), user.pk)

    def test_the_response_is_the_me_payload(self):
        code = self.request_and_read_code(EMAIL)
        verify_body = self.verify(EMAIL, code, accepts_terms=True).json()
        me_body = self.client.get(ME_URL).json()

        self.assertEqual(
            sorted(verify_body),
            ["created", "lang", "membership_role", "memberships", "org", "user"],
        )
        for key in ("user", "org", "membership_role", "memberships", "lang"):
            self.assertEqual(verify_body[key], me_body[key], key)

    def test_a_workspace_is_bootstrapped(self):
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True)
        user = User.objects.get(email=EMAIL)
        memberships = list(Membership.objects.for_user(user))
        self.assertEqual(len(memberships), 1)
        self.assertEqual(memberships[0].role, Role.OWNER)
        self.assertEqual(Organization.objects.count(), 1)

    def test_a_second_login_reuses_the_workspace(self):
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True)
        self.client.post("/api/v1/auth/logout/")

        from django.core.cache import cache

        cache.clear()
        second = self.request_and_read_code(EMAIL)
        response = self.verify(EMAIL, second)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["created"])
        self.assertEqual(Organization.objects.count(), 1)

    def test_the_language_is_stored_on_the_user(self):
        code = self.request_and_read_code(EMAIL, lang="de")
        self.verify(EMAIL, code, accepts_terms=True, lang="de")
        self.assertEqual(User.objects.get(email=EMAIL).lang, "de")

    def test_the_browser_language_is_used_when_the_client_sends_none(self):
        # web/api.js's verifyCode() sends no `lang` — that is the pinned
        # contract — so without this fallback every real registration would be
        # stamped "en".
        code = self.request_and_read_code(EMAIL, lang="de")
        self.post_json(
            VERIFY_URL,
            {"email": EMAIL, "code": code, "accepts_terms": True},
            HTTP_ACCEPT_LANGUAGE="de-DE,de;q=0.9",
        )
        self.assertEqual(User.objects.get(email=EMAIL).lang, "de")

    def test_a_browser_header_never_overwrites_a_stored_preference(self):
        User.objects.create_user(email=EMAIL, lang="ro", is_verified=True)
        code = self.request_and_read_code(EMAIL)
        self.post_json(
            VERIFY_URL,
            {"email": EMAIL, "code": code},
            HTTP_ACCEPT_LANGUAGE="en-GB,en;q=0.9",
        )
        self.assertEqual(User.objects.get(email=EMAIL).lang, "ro")

    def test_an_explicit_language_does_overwrite_a_stored_preference(self):
        User.objects.create_user(email=EMAIL, lang="ro", is_verified=True)
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, lang="de")
        self.assertEqual(User.objects.get(email=EMAIL).lang, "de")

    def test_a_pasted_code_with_separators_is_accepted(self):
        code = self.request_and_read_code(EMAIL)
        spaced = f"{code[:3]} {code[3:]}"
        self.assertEqual(
            self.verify(EMAIL, spaced, accepts_terms=True).status_code, 200
        )

    def test_the_address_is_matched_case_insensitively(self):
        code = self.request_and_read_code(EMAIL)
        self.assertEqual(
            self.verify(EMAIL.upper(), code, accepts_terms=True).status_code, 200
        )


class SingleUseTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        seed_legal_documents()

    def test_a_code_burns_on_use(self):
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True)
        otp = EmailOTP.objects.get()
        self.assertIsNotNone(otp.consumed_at)

    def test_replaying_a_consumed_code_fails(self):
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True)
        replay = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(replay.status_code, 400)
        self.assertEqual(replay.json()["error"]["code"], "code_expired")

    def test_an_expired_code_reports_code_expired(self):
        otp, code = services.issue_code(email=EMAIL)
        EmailOTP.objects.filter(pk=otp.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "code_expired")

    def test_an_unrelated_wrong_code_reports_invalid_code(self):
        services.issue_code(email=EMAIL)
        response = self.verify(EMAIL, "000000", accepts_terms=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_code")

    def test_a_code_for_a_different_address_does_not_verify(self):
        _, code = services.issue_code(email="other@example.de")
        response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_code")


class AttemptCapTests(AuthTestCase):
    """Spec §4: five wrong guesses kill the ROW, not the account."""

    def setUp(self):
        super().setUp()
        seed_legal_documents()

    def _wrong(self, real_code: str) -> str:
        return "000000" if real_code != "000000" else "111111"

    def test_the_fifth_wrong_guess_burns_the_code(self):
        otp, code = services.issue_code(email=EMAIL)
        wrong = self._wrong(code)
        for attempt in range(MAX_OTP_ATTEMPTS - 1):
            response = self.verify(EMAIL, wrong)
            self.assertEqual(response.json()["error"]["code"], "invalid_code", attempt)

        final = self.verify(EMAIL, wrong)
        self.assertEqual(final.status_code, 429)
        self.assertEqual(final.json()["error"]["code"], "too_many_attempts")

        otp.refresh_from_db()
        self.assertEqual(otp.attempts, MAX_OTP_ATTEMPTS)

    def test_a_burned_row_stops_accepting_the_right_code(self):
        _, code = services.issue_code(email=EMAIL)
        wrong = self._wrong(code)
        for _ in range(MAX_OTP_ATTEMPTS):
            self.verify(EMAIL, wrong)
        response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(response.json()["error"]["code"], "too_many_attempts")
        self.assertFalse(User.objects.filter(email=EMAIL).exists())

    def test_attempts_never_exceed_the_check_constraint(self):
        _, code = services.issue_code(email=EMAIL)
        wrong = self._wrong(code)
        for _ in range(MAX_OTP_ATTEMPTS + 3):
            self.verify(EMAIL, wrong)
        self.assertEqual(EmailOTP.objects.get().attempts, MAX_OTP_ATTEMPTS)

    def test_a_wrong_guess_charges_every_live_code(self):
        # The credential is the whole live set, because verify compares against
        # the whole live set. Charging only the newest would leave the older
        # ones guessable forever.
        services.issue_code(email=EMAIL)
        services.issue_code(email=EMAIL)
        self.verify(EMAIL, "000000")
        self.assertEqual(
            list(EmailOTP.objects.values_list("attempts", flat=True)), [1, 1]
        )

    def test_the_account_itself_is_never_locked(self):
        # Spec §5: what dies is the credential, not the identity. Burning a
        # code to death must still leave the mailbox owner able to log in with
        # the next one — that is what "forces the attacker back to the inbox"
        # means, and it is why there is no per-email lockout anywhere.
        services.issue_code(email=EMAIL)
        for _ in range(MAX_OTP_ATTEMPTS):
            self.verify(EMAIL, "000000")
        self.assertEqual(EmailOTP.objects.get().attempts, MAX_OTP_ATTEMPTS)

        _, fresh = services.issue_code(email=EMAIL)
        response = self.verify(EMAIL, fresh, accepts_terms=True)
        self.assertEqual(response.status_code, 200)


class ThreeLiveCodesTests(AuthTestCase):
    """Spec §4: the greylisting scenario — the user types the *older* code."""

    def setUp(self):
        super().setUp()
        seed_legal_documents()

    def test_the_oldest_of_three_live_codes_still_verifies(self):
        _, oldest = services.issue_code(email=EMAIL)
        services.issue_code(email=EMAIL)
        services.issue_code(email=EMAIL)
        response = self.verify(EMAIL, oldest, accepts_terms=True)
        self.assertEqual(response.status_code, 200)

    def test_using_one_code_leaves_the_others_live(self):
        _, first = services.issue_code(email=EMAIL)
        _, second = services.issue_code(email=EMAIL)
        self.verify(EMAIL, first, accepts_terms=True)
        live = list(EmailOTP.objects.live(email=EMAIL))
        self.assertEqual(len(live), 1)
        self.assertTrue(live[0].matches(second))

    def test_comparison_is_constant_time(self):
        _, code = services.issue_code(email=EMAIL)
        with mock.patch(
            "django.utils.crypto.constant_time_compare", wraps=None
        ) as patched:
            patched.side_effect = lambda a, b: a == b
            self.verify(EMAIL, code, accepts_terms=True)
        self.assertTrue(patched.called)

    def test_every_live_row_is_compared_even_after_a_match(self):
        # No short-circuit: the time taken must not reveal *which* code matched.
        _, oldest = services.issue_code(email=EMAIL)
        services.issue_code(email=EMAIL)
        services.issue_code(email=EMAIL)
        with mock.patch.object(
            EmailOTP, "matches", autospec=True, side_effect=EmailOTP.matches
        ) as patched:
            self.verify(EMAIL, oldest, accepts_terms=True)
        # Three live rows -> three comparisons, whichever one matched.
        self.assertEqual(patched.call_count, 3)


class ConsentTests(AuthTestCase):
    """Spec §10: affirmative consent, resolved server-side."""

    def test_first_login_without_the_checkbox_is_400_terms_required(self):
        seed_legal_documents()
        code = self.request_and_read_code(EMAIL)
        response = self.verify(EMAIL, code, accepts_terms=False)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "terms_required")
        self.assertFalse(User.objects.filter(email=EMAIL).exists())

    def test_a_terms_required_rejection_does_not_burn_the_code(self):
        # Otherwise ticking the box costs the user a whole new email round trip.
        seed_legal_documents()
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=False)
        otp = EmailOTP.objects.get()
        self.assertIsNone(otp.consumed_at)
        self.assertEqual(otp.attempts, 0)
        self.assertEqual(self.verify(EMAIL, code, accepts_terms=True).status_code, 200)

    def test_an_existing_user_does_not_have_to_re_accept(self):
        seed_legal_documents()
        User.objects.create_user(email=EMAIL, is_verified=True)
        code = self.request_and_read_code(EMAIL)
        self.assertEqual(self.verify(EMAIL, code).status_code, 200)

    def test_consent_records_are_written_for_terms_and_privacy(self):
        seed_legal_documents()
        from apps.legal.models import ConsentRecord

        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True)
        types = set(
            ConsentRecord.objects.filter(
                user=User.objects.get(email=EMAIL)
            ).values_list("type", flat=True)
        )
        self.assertEqual(types, {"terms", "privacy"})

    def test_the_recorded_version_comes_from_the_server_not_the_client(self):
        seed_legal_documents()
        from apps.legal.models import ConsentRecord

        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True)
        versions = set(ConsentRecord.objects.values_list("doc_version", flat=True))
        self.assertEqual(versions, {TERMS_VERSION})

    def test_a_stale_client_version_is_409_terms_outdated(self):
        seed_legal_documents()
        code = self.request_and_read_code(EMAIL)
        response = self.verify(
            EMAIL, code, accepts_terms=True, terms_version="1999-01-01"
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "terms_outdated")

    def test_a_terms_outdated_rejection_rolls_the_burn_back(self):
        seed_legal_documents()
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True, terms_version="1999-01-01")
        self.assertIsNone(EmailOTP.objects.get().consumed_at)
        self.assertFalse(User.objects.filter(email=EMAIL).exists())

    def test_unseeded_legal_documents_fail_closed(self):
        # No seed_legal_documents() call here on purpose.
        code = self.request_and_read_code(EMAIL)
        response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(User.objects.filter(email=EMAIL).exists())

    def test_a_missing_legal_service_does_not_break_login(self):
        # apps.legal is built by a parallel work stream; a half-built one must
        # degrade loudly rather than make login impossible.
        code = self.request_and_read_code(EMAIL)
        with mock.patch("apps.legal.services.record_registration_consent", None):
            with self.assertLogs("findajob.accounts", level="ERROR"):
                response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(response.status_code, 200)


class AccountStateTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        seed_legal_documents()

    def test_a_deactivated_account_cannot_sign_in(self):
        User.objects.create_user(email=EMAIL, is_active=False)
        code = self.request_and_read_code(EMAIL)
        response = self.verify(EMAIL, code)
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_verification_flag_is_set_on_a_previously_unverified_account(self):
        User.objects.create_user(email=EMAIL)
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code)
        self.assertTrue(User.objects.get(email=EMAIL).is_verified)


class VerifyRateLimitTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        seed_legal_documents()

    def test_the_subnet_budget_returns_429_with_retry_after(self):
        services.issue_code(email=EMAIL)
        for _ in range(ratelimit.VERIFY_FAILURE_LIMIT):
            self.verify(EMAIL, "000000")
        blocked = self.verify(EMAIL, "000000")
        self.assertEqual(blocked.status_code, 429)
        error = blocked.json()["error"]
        self.assertEqual(error["code"], "rate_limited")
        self.assertGreaterEqual(error["retry_after"], 1)
        self.assertEqual(blocked["Retry-After"], str(error["retry_after"]))

    def test_a_successful_login_clears_the_budget(self):
        # Nine wrong guesses leave the budget one short of blocking. Note the
        # first code dies at five attempts long before the budget bites — the
        # per-row cap is the primary control and the subnet budget only catches
        # an attacker who keeps requesting fresh codes.
        services.issue_code(email=EMAIL)
        for _ in range(ratelimit.VERIFY_FAILURE_LIMIT - 1):
            self.verify(EMAIL, "000000")

        _, fresh = services.issue_code(email=EMAIL)
        self.assertEqual(self.verify(EMAIL, fresh, accepts_terms=True).status_code, 200)
        self.assertTrue(ratelimit.check_verify_budget(EMAIL, "127.0.0.1").allowed)

    def test_a_dead_cache_fails_closed(self):
        # Spec §5: verify is the one budget that fails CLOSED, because an
        # unmetered verify is an online oracle against a 10^6 keyspace.
        from tests.accounts.test_ratelimit import BrokenCache

        _, code = services.issue_code(email=EMAIL)
        with mock.patch.object(ratelimit, "cache", BrokenCache()):
            response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(response.status_code, 429)
        self.assertGreaterEqual(response.json()["error"]["retry_after"], 1)

    def test_a_dead_cache_never_500s(self):
        from tests.accounts.test_ratelimit import BrokenCache

        with mock.patch.object(ratelimit, "cache", BrokenCache()):
            response = self.verify(EMAIL, "000000")
        self.assertLess(response.status_code, 500)


class VerifyValidationTests(AuthTestCase):
    def test_a_malformed_code_is_400_invalid_code(self):
        for bad in ("", "12345", "1234567", "abcdef", "12 34"):
            with self.subTest(bad=bad):
                response = self.verify(EMAIL, bad)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_code")

    def test_a_malformed_address_is_400_invalid_email(self):
        response = self.verify("not-an-email", "123456")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_email")

    def test_a_malformed_code_costs_no_attempt(self):
        services.issue_code(email=EMAIL)
        self.verify(EMAIL, "abcdef")
        self.assertEqual(EmailOTP.objects.get().attempts, 0)


class VerifyCsrfTests(AuthTestCase):
    def test_a_post_without_a_token_is_rejected(self):
        seed_legal_documents()
        _, code = services.issue_code(email=EMAIL)
        client = Client(enforce_csrf_checks=True)
        response = client.post(
            VERIFY_URL,
            {"email": EMAIL, "code": code, "accepts_terms": True},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")
        self.assertFalse(User.objects.filter(email=EMAIL).exists())

    def test_a_post_with_a_token_succeeds(self):
        seed_legal_documents()
        _, code = services.issue_code(email=EMAIL)
        client, token = csrf_client()
        response = client.post(
            VERIFY_URL,
            {"email": EMAIL, "code": code, "accepts_terms": True},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)

    def test_the_csrf_token_is_rotated_on_login(self):
        """Regression guard for a genuinely invisible bug.

        ``login()`` rotates the CSRF token by setting
        ``request.csrf_cookie_needs_reset``. DRF's ``Request`` proxies attribute
        *reads* to the real ``HttpRequest`` but not writes, so handing it the
        DRF request parks that flag on the wrapper, where
        ``CsrfViewMiddleware.process_response`` never looks — and the token is
        silently never rotated. Nothing else fails: the old token stays valid,
        so no test would notice.
        """
        seed_legal_documents()
        _, code = services.issue_code(email=EMAIL)
        client, token = csrf_client()
        response = client.post(
            VERIFY_URL,
            {"email": EMAIL, "code": code, "accepts_terms": True},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=token,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", response.cookies)
        self.assertNotEqual(response.cookies["csrftoken"].value, token)
        # ...and the rotated token is immediately usable.
        self.assertEqual(
            client.post(
                "/api/v1/auth/logout/",
                HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
            ).status_code,
            204,
        )


class SecrecyTests(AuthTestCase):
    def test_the_code_never_reaches_a_log_during_verification(self):
        import logging

        seed_legal_documents()
        _, code = services.issue_code(email=EMAIL)
        with self.assertLogs("findajob", level="DEBUG") as captured:
            self.verify(EMAIL, code, accepts_terms=True)
            logging.getLogger("findajob").info("marker")
        self.assertNotIn(code, "\n".join(captured.output))

    def test_the_response_never_echoes_the_code(self):
        seed_legal_documents()
        _, code = services.issue_code(email=EMAIL)
        response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertNotIn(code, response.content.decode())


class AuditTrailTests(AuthTestCase):
    """Login events reach ``AuditLog`` via ``apps.tenancy.services.record_security_event``."""

    def setUp(self):
        super().setUp()
        seed_legal_documents()

    def test_a_successful_login_writes_a_login_succeeded_event(self):
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True)

        user = User.objects.get(email=EMAIL)
        membership = Membership.objects.for_user(user).get()
        row = AuditLog.all_objects.get()
        self.assertEqual(row.action, "auth.login_succeeded")
        self.assertEqual(row.actor_id, user.pk)
        self.assertEqual(row.org_id, membership.org_id)
        self.assertEqual(row.ip, "127.0.0.1")
        self.assertEqual(row.target_type, "accounts.emailotp")

    def test_a_wrong_code_writes_a_login_failed_event_with_no_org(self):
        services.issue_code(email=EMAIL)
        self.verify(EMAIL, "000000")

        row = AuditLog.all_objects.get()
        self.assertEqual(row.action, "auth.login_failed")
        self.assertIsNone(row.org_id)
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.target_type, "email")
        self.assertEqual(row.target_id, EMAIL)
        self.assertEqual(row.payload["reason"], "invalid_code")

    def test_exhausting_a_code_writes_a_too_many_attempts_reason(self):
        services.issue_code(email=EMAIL)
        for _ in range(MAX_OTP_ATTEMPTS):
            self.verify(EMAIL, "000000")

        rows = list(AuditLog.all_objects.order_by("id"))
        self.assertEqual(len(rows), MAX_OTP_ATTEMPTS)
        self.assertEqual(rows[-1].payload["reason"], "too_many_attempts")

    def test_the_subnet_rate_limit_writes_a_rate_limited_reason(self):
        services.issue_code(email=EMAIL)
        for _ in range(ratelimit.VERIFY_FAILURE_LIMIT):
            self.verify(EMAIL, "000000")
        self.verify(EMAIL, "000000")

        newest = AuditLog.all_objects.first()
        self.assertEqual(newest.payload["reason"], "rate_limited")

    def test_a_broken_audit_log_never_blocks_a_successful_login(self):
        from unittest import mock

        code = self.request_and_read_code(EMAIL)
        with mock.patch(
            "apps.tenancy.models.AuditLog.objects.record",
            side_effect=RuntimeError("db is down"),
        ):
            response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(response.status_code, 200)
