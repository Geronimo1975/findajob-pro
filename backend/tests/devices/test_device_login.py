"""``POST /api/v1/auth/device-login/`` and the ``remember_device`` opt-in.

See ``docs/2026-08-22-trusted-device-spec.md`` §§4–6.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.utils import timezone

from apps.accounts import ratelimit
from apps.accounts.models import User
from apps.devices.models import TrustedDevice
from apps.devices.services import DEVICE_COOKIE_NAME, build_cookie_value, trust_device
from apps.tenancy.models import AuditLog
from tests.accounts.support import AuthTestCase, seed_legal_documents

EMAIL = "george@example.de"
DEVICE_URL = "/api/v1/auth/device-login/"
ME_URL = "/api/v1/me/"


class RememberDeviceTests(AuthTestCase):
    """Minting happens only on an explicit opt-in (spec §6)."""

    def setUp(self):
        super().setUp()
        seed_legal_documents()

    def test_no_device_is_minted_by_default(self):
        code = self.request_and_read_code(EMAIL)
        response = self.verify(EMAIL, code, accepts_terms=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TrustedDevice.objects.count(), 0)
        self.assertNotIn(DEVICE_COOKIE_NAME, response.cookies)

    def test_remember_device_mints_one_and_sets_the_cookie(self):
        code = self.request_and_read_code(EMAIL)
        response = self.verify(
            EMAIL, code, accepts_terms=True, remember_device=True
        )
        self.assertEqual(response.status_code, 200)

        device = TrustedDevice.objects.get()
        self.assertEqual(device.user.email, EMAIL)
        cookie = response.cookies[DEVICE_COOKIE_NAME]
        self.assertTrue(cookie.value.startswith(str(device.device_id)))
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")

    def test_the_cookie_secret_is_not_the_stored_hash(self):
        code = self.request_and_read_code(EMAIL)
        response = self.verify(
            EMAIL, code, accepts_terms=True, remember_device=True
        )
        secret = response.cookies[DEVICE_COOKIE_NAME].value.split(".", 1)[1]
        self.assertNotEqual(secret, TrustedDevice.objects.get().secret_hash)

    def test_minting_writes_an_audit_row(self):
        code = self.request_and_read_code(EMAIL)
        self.verify(EMAIL, code, accepts_terms=True, remember_device=True)
        actions = set(AuditLog.all_objects.values_list("action", flat=True))
        self.assertIn("auth.device_trusted", actions)

    def test_the_secret_never_appears_in_the_response_body(self):
        code = self.request_and_read_code(EMAIL)
        response = self.verify(
            EMAIL, code, accepts_terms=True, remember_device=True
        )
        secret = response.cookies[DEVICE_COOKIE_NAME].value.split(".", 1)[1]
        self.assertNotIn(secret, response.content.decode())

    def test_a_failed_cookie_write_leaves_no_orphaned_device_row(self):
        """Regression: the row must not outlive the cookie that proves it.

        Before the fix, ``trust_device()`` committing the row before
        ``set_device_cookie()`` ran meant a cookie-write failure left an
        unrevoked ``TrustedDevice`` sitting in the DB for the full 30-day TTL
        — one the browser could never present, since it never got the
        cookie — with no audit trail explaining why. The login itself must
        still succeed (spec: never turn a successful login into an error).
        """
        code = self.request_and_read_code(EMAIL)
        with mock.patch(
            "apps.devices.services.set_device_cookie",
            side_effect=RuntimeError("cookie write blew up"),
        ):
            response = self.verify(
                EMAIL, code, accepts_terms=True, remember_device=True
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TrustedDevice.objects.count(), 0)
        self.assertNotIn(DEVICE_COOKIE_NAME, response.cookies)

        row = AuditLog.all_objects.filter(action="auth.device_trust_failed").get()
        self.assertIn("device_id", row.payload)


class DeviceLoginHappyPathTests(AuthTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email=EMAIL, is_verified=True)
        from apps.tenancy.services import bootstrap_workspace

        self.membership = bootstrap_workspace(self.user)
        self.device, self.secret = trust_device(user=self.user)

    def _present_cookie(self, value: str | None = None) -> None:
        self.client.cookies[DEVICE_COOKIE_NAME] = value or build_cookie_value(
            self.device, self.secret
        )

    def test_a_valid_cookie_mints_a_session(self):
        self._present_cookie()
        response = self.post_json(DEVICE_URL, {"email": EMAIL})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(int(self.client.session["_auth_user_id"]), self.user.pk)

    def test_the_response_is_the_me_payload(self):
        self._present_cookie()
        body = self.post_json(DEVICE_URL, {"email": EMAIL}).json()
        me = self.client.get(ME_URL).json()
        for key in ("user", "org", "membership_role", "memberships", "lang"):
            self.assertEqual(body[key], me[key], key)

    def test_no_email_is_sent(self):
        from django.core import mail

        self._present_cookie()
        self.post_json(DEVICE_URL, {"email": EMAIL})
        self.assertEqual(len(mail.outbox), 0)

    def test_it_records_last_used(self):
        self._present_cookie()
        self.post_json(DEVICE_URL, {"email": EMAIL})
        self.device.refresh_from_db()
        self.assertIsNotNone(self.device.last_used_at)

    def test_it_writes_an_audit_row(self):
        self._present_cookie()
        self.post_json(DEVICE_URL, {"email": EMAIL})
        row = AuditLog.all_objects.filter(action="auth.device_login_succeeded").get()
        self.assertEqual(row.actor_id, self.user.pk)
        self.assertEqual(row.org_id, self.membership.org_id)

    def test_it_creates_no_account_and_no_second_workspace(self):
        from apps.tenancy.models import Organization

        self._present_cookie()
        self.post_json(DEVICE_URL, {"email": EMAIL})
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(Organization.objects.count(), 1)


class DeviceLoginRejectionTests(AuthTestCase):
    """Every failure is the same 401 with the same code (spec §4)."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email=EMAIL, is_verified=True)
        from apps.tenancy.services import bootstrap_workspace

        bootstrap_workspace(self.user)
        self.device, self.secret = trust_device(user=self.user)

    def _assert_rejected(self, response) -> None:
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "device_not_trusted")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_no_cookie_at_all(self):
        self._assert_rejected(self.post_json(DEVICE_URL, {"email": EMAIL}))

    def test_a_malformed_cookie(self):
        self.client.cookies[DEVICE_COOKIE_NAME] = "no-separator-here"
        self._assert_rejected(self.post_json(DEVICE_URL, {"email": EMAIL}))

    def test_a_cookie_whose_id_is_not_a_uuid(self):
        self.client.cookies[DEVICE_COOKIE_NAME] = f"not-a-uuid.{self.secret}"
        self._assert_rejected(self.post_json(DEVICE_URL, {"email": EMAIL}))

    def test_an_unknown_device_id(self):
        import uuid

        self.client.cookies[DEVICE_COOKIE_NAME] = f"{uuid.uuid4()}.{self.secret}"
        self._assert_rejected(self.post_json(DEVICE_URL, {"email": EMAIL}))

    def test_a_wrong_secret(self):
        self.client.cookies[DEVICE_COOKIE_NAME] = f"{self.device.device_id}.wrong"
        self._assert_rejected(self.post_json(DEVICE_URL, {"email": EMAIL}))

    def test_an_expired_device(self):
        TrustedDevice.objects.filter(pk=self.device.pk).update(
            expires_at=timezone.now() - timedelta(seconds=1)
        )
        self.client.cookies[DEVICE_COOKIE_NAME] = build_cookie_value(
            self.device, self.secret
        )
        self._assert_rejected(self.post_json(DEVICE_URL, {"email": EMAIL}))

    def test_a_revoked_device(self):
        from apps.devices.services import revoke_device

        revoke_device(self.device)
        self.client.cookies[DEVICE_COOKIE_NAME] = build_cookie_value(
            self.device, self.secret
        )
        self._assert_rejected(self.post_json(DEVICE_URL, {"email": EMAIL}))

    def test_a_deactivated_user(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.client.cookies[DEVICE_COOKIE_NAME] = build_cookie_value(
            self.device, self.secret
        )
        self._assert_rejected(self.post_json(DEVICE_URL, {"email": EMAIL}))

    def test_an_email_that_is_not_the_owner(self):
        self.client.cookies[DEVICE_COOKIE_NAME] = build_cookie_value(
            self.device, self.secret
        )
        self._assert_rejected(
            self.post_json(DEVICE_URL, {"email": "someone-else@example.de"})
        )

    def test_a_rejection_clears_the_stale_cookie(self):
        self.client.cookies[DEVICE_COOKIE_NAME] = "garbage.value"
        response = self.post_json(DEVICE_URL, {"email": EMAIL})
        self.assertEqual(response.cookies[DEVICE_COOKIE_NAME].value, "")

    def test_a_rejection_is_audited(self):
        self.client.cookies[DEVICE_COOKIE_NAME] = f"{self.device.device_id}.wrong"
        self.post_json(DEVICE_URL, {"email": EMAIL})
        row = AuditLog.all_objects.filter(action="auth.device_login_failed").get()
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.payload["reason"], "bad_device")

    def test_no_cookie_at_all_is_not_audited_or_counted(self):
        # The ordinary "no remembered device" path must not look like an
        # attack: no security event, and it must not spend the fail-closed
        # device-login budget (see DeviceLoginRateLimitTests below).
        self.post_json(DEVICE_URL, {"email": EMAIL})
        self.assertFalse(
            AuditLog.all_objects.filter(action="auth.device_login_failed").exists()
        )

    def test_a_malformed_email_is_invalid_email_not_device_not_trusted(self):
        response = self.post_json(DEVICE_URL, {"email": "not-an-email"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_email")


class DeviceLoginRateLimitTests(AuthTestCase):
    """Its own budget, never the verify one (spec §5)."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email=EMAIL, is_verified=True)
        from apps.tenancy.services import bootstrap_workspace

        bootstrap_workspace(self.user)
        self.device, self.secret = trust_device(user=self.user)

    def test_repeated_failures_eventually_return_429(self):
        # Must actually present a device cookie: the budget defends against
        # cookie-guessing, so only a presented-but-wrong cookie should spend it.
        # Each rejection clears the cookie from the response, so it has to be
        # re-presented before every attempt, exactly as a guessing script would.
        for _ in range(ratelimit.DEVICE_LOGIN_FAILURE_LIMIT):
            self.client.cookies[DEVICE_COOKIE_NAME] = f"{self.device.device_id}.wrong"
            self.post_json(DEVICE_URL, {"email": EMAIL})
        self.client.cookies[DEVICE_COOKIE_NAME] = f"{self.device.device_id}.wrong"
        blocked = self.post_json(DEVICE_URL, {"email": EMAIL})
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(blocked.json()["error"]["code"], "rate_limited")

    def test_cookie_less_requests_never_exhaust_the_device_budget(self):
        # Root-cause regression: ordinary logins without a remembered device
        # (shared IPs, offices, first-time visitors) must never be able to
        # trip a budget sized for cookie-guessing attacks.
        for _ in range(ratelimit.DEVICE_LOGIN_FAILURE_LIMIT * 3):
            response = self.post_json(DEVICE_URL, {"email": EMAIL})
            self.assertEqual(response.status_code, 401)
        self.assertTrue(ratelimit.check_device_login_budget(EMAIL, "127.0.0.1").allowed)

    def test_burning_the_device_budget_leaves_the_otp_budget_alone(self):
        # The DoS this separation exists to prevent: junk device cookies must
        # not be able to lock the owner out of the normal OTP login.
        for _ in range(ratelimit.DEVICE_LOGIN_FAILURE_LIMIT * 2):
            self.post_json(DEVICE_URL, {"email": EMAIL})
        self.assertTrue(ratelimit.check_verify_budget(EMAIL, "127.0.0.1").allowed)

    def test_a_successful_device_login_clears_its_budget(self):
        for _ in range(ratelimit.DEVICE_LOGIN_FAILURE_LIMIT - 1):
            self.post_json(DEVICE_URL, {"email": EMAIL})
        self.client.cookies[DEVICE_COOKIE_NAME] = build_cookie_value(
            self.device, self.secret
        )
        self.assertEqual(self.post_json(DEVICE_URL, {"email": EMAIL}).status_code, 200)
        self.assertTrue(ratelimit.check_device_login_budget(EMAIL, "127.0.0.1").allowed)


class LogoutKeepsTheDeviceTests(AuthTestCase):
    """Spec §6: logout ends the session and nothing else."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user(email=EMAIL, is_verified=True)
        from apps.tenancy.services import bootstrap_workspace

        bootstrap_workspace(self.user)
        self.device, self.secret = trust_device(user=self.user)

    def test_logout_does_not_revoke_the_device(self):
        self.client.force_login(self.user)
        self.client.post("/api/v1/auth/logout/")
        self.device.refresh_from_db()
        self.assertIsNone(self.device.revoked_at)

    def test_the_device_still_logs_in_after_a_logout(self):
        self.client.force_login(self.user)
        self.client.post("/api/v1/auth/logout/")
        self.client.cookies[DEVICE_COOKIE_NAME] = build_cookie_value(
            self.device, self.secret
        )
        self.assertEqual(self.post_json(DEVICE_URL, {"email": EMAIL}).status_code, 200)
