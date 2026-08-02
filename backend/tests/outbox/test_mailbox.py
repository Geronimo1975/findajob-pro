"""Outbox mailbox endpoints — docs/2026-08-02-outbox-spec.md."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.outbox import crypto
from apps.outbox.models import MailboxConnection, MailboxStatus
from apps.outbox.services import SmtpCheckError
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user

URL = "/api/v1/outbox/mailbox/"
TEST_URL = "/api/v1/outbox/mailbox/test/"

SMTP_BODY = {
    "provider": "smtp",
    "email_address": "ion@gmx.de",
    "display_name": "Ion Popescu",
    "smtp": {
        "host": "mail.gmx.net",
        "port": 587,
        "username": "ion@gmx.de",
        "password": "app-password",
        "use_tls": True,
    },
}


class AnonymousTests(TestCase):
    def test_anonymous_is_401_with_envelope(self):
        response = APIClient().get(URL)
        self.assertEqual(response.status_code, 401)
        self.assertIn("error", response.json())


class MailboxLifecycleTests(TestCase):
    def setUp(self):
        self.user = make_user()
        bootstrap_workspace(self.user)
        self.client = APIClient()
        self.client.force_login(self.user)

    def _connect(self):
        with mock.patch("apps.outbox.views.verify_smtp"):
            return self.client.post(URL, SMTP_BODY, format="json")

    def test_get_without_mailbox_is_404_mailbox_not_connected(self):
        response = self.client.get(URL)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["error"]["code"], "mailbox_not_connected"
        )

    def test_connect_verifies_then_stores_and_returns_no_secrets(self):
        response = self._connect()
        self.assertEqual(response.status_code, 201)
        data = response.json()
        self.assertEqual(data["email_address"], "ion@gmx.de")
        self.assertEqual(data["status"], "connected")
        self.assertIsNone(data["last_error_code"])
        flat = str(data)
        self.assertNotIn("app-password", flat)
        self.assertNotIn("credentials", flat)

    def test_credentials_are_encrypted_at_rest_and_round_trip(self):
        self._connect()
        row = MailboxConnection.all_objects.get()
        self.assertNotIn(b"app-password", bytes(row.credentials_encrypted))
        self.assertEqual(
            crypto.decrypt_credentials(row.credentials_encrypted)["password"],
            "app-password",
        )

    def test_failed_verification_stores_nothing(self):
        with mock.patch(
            "apps.outbox.views.verify_smtp",
            side_effect=SmtpCheckError("auth_failed", "bad login"),
        ):
            response = self.client.post(URL, SMTP_BODY, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["error"]["code"], "smtp_verification_failed"
        )
        self.assertEqual(MailboxConnection.all_objects.count(), 0)

    def test_reconnect_updates_in_place_one_mailbox_per_member(self):
        self._connect()
        body = dict(SMTP_BODY, email_address="ion@web.de")
        with mock.patch("apps.outbox.views.verify_smtp"):
            self.client.post(URL, body, format="json")
        self.assertEqual(MailboxConnection.all_objects.count(), 1)
        self.assertEqual(
            MailboxConnection.all_objects.get().email_address, "ion@web.de"
        )

    def test_non_smtp_provider_is_rejected_for_now(self):
        response = self.client.post(
            URL, dict(SMTP_BODY, provider="gmail"), format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_delete_removes_the_row_and_its_secrets(self):
        self._connect()
        response = self.client.delete(URL)
        self.assertEqual(response.status_code, 204)
        self.assertEqual(MailboxConnection.all_objects.count(), 0)

    def test_delete_without_mailbox_is_404(self):
        self.assertEqual(self.client.delete(URL).status_code, 404)

    def test_test_send_success_is_202_and_marks_verified(self):
        self._connect()
        with mock.patch("apps.outbox.views.send_test_email") as send:
            response = self.client.post(TEST_URL)
        self.assertEqual(response.status_code, 202)
        send.assert_called_once()
        # Secrets were decrypted for the send, not passed encrypted.
        self.assertEqual(send.call_args.args[0]["password"], "app-password")
        row = MailboxConnection.all_objects.get()
        self.assertEqual(row.status, MailboxStatus.CONNECTED)
        self.assertIsNotNone(row.last_verified_at)

    def test_test_send_failure_is_502_and_marks_error(self):
        self._connect()
        with mock.patch(
            "apps.outbox.views.send_test_email",
            side_effect=SmtpCheckError("send_failed", "boom"),
        ):
            response = self.client.post(TEST_URL)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"]["code"], "send_failed")
        row = MailboxConnection.all_objects.get()
        self.assertEqual(row.status, MailboxStatus.ERROR)
        self.assertEqual(row.last_error_code, "send_failed")


class TenantIsolationTests(TestCase):
    """A member sees only their own mailbox, in their own workspace."""

    def setUp(self):
        self.alice = make_user()
        self.alice_org = bootstrap_workspace(self.alice).org
        self.bob = make_user()
        bootstrap_workspace(self.bob)
        MailboxConnection.objects.create(
            org=self.alice_org,
            user=self.alice,
            provider="smtp",
            email_address="alice@example.de",
            credentials_encrypted=crypto.encrypt_credentials({"password": "x"}),
        )

    def test_other_user_does_not_see_the_mailbox(self):
        client = APIClient()
        client.force_login(self.bob)
        response = client.get(URL)
        self.assertEqual(response.status_code, 404)

    def test_naming_a_foreign_org_is_403(self):
        client = APIClient()
        client.force_login(self.bob)
        response = client.get(URL, {"org": self.alice_org.pk})
        self.assertEqual(response.status_code, 403)
