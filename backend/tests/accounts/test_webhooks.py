"""``POST /webhooks/email/brevo/`` -> ``SuppressedAddress`` (spec §6)."""

from __future__ import annotations

import base64
import json

from django.test import TestCase, override_settings

from apps.accounts.models import SuppressedAddress, SuppressionReason
from apps.accounts.webhooks import suppression_for

URL = "/webhooks/email/brevo/"
SECRET = "brevo:s3cret"
AUTH = "Basic " + base64.b64encode(SECRET.encode()).decode()


def brevo_payload(event: str, email: str = "bounced@example.de") -> bytes:
    return json.dumps(
        {
            "event": event,
            "email": email,
            "id": 1,
            "message-id": "<abc@example.de>",
            "ts_event": 1_785_000_000,
        }
    ).encode()


class FailClosedTests(TestCase):
    """Anymail only *warns* without a secret; this endpoint must refuse."""

    @override_settings(ANYMAIL={"WEBHOOK_SECRET": ""})
    def test_no_configured_secret_is_503(self):
        response = self.client.post(
            URL, brevo_payload("hard_bounce"), content_type="application/json"
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(SuppressedAddress.objects.count(), 0)

    @override_settings(ANYMAIL={"WEBHOOK_SECRET": SECRET})
    def test_a_missing_authorization_header_is_rejected(self):
        response = self.client.post(
            URL, brevo_payload("hard_bounce"), content_type="application/json"
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SuppressedAddress.objects.count(), 0)

    @override_settings(ANYMAIL={"WEBHOOK_SECRET": SECRET})
    def test_a_wrong_secret_is_rejected(self):
        wrong = "Basic " + base64.b64encode(b"brevo:nope").decode()
        response = self.client.post(
            URL,
            brevo_payload("hard_bounce"),
            content_type="application/json",
            HTTP_AUTHORIZATION=wrong,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(SuppressedAddress.objects.count(), 0)


@override_settings(ANYMAIL={"WEBHOOK_SECRET": SECRET})
class SuppressionTests(TestCase):
    def post(self, event: str, email: str = "bounced@example.de"):
        return self.client.post(
            URL,
            brevo_payload(event, email),
            content_type="application/json",
            HTTP_AUTHORIZATION=AUTH,
        )

    def test_a_hard_bounce_suppresses(self):
        response = self.post("hard_bounce")
        self.assertEqual(response.status_code, 200)
        row = SuppressedAddress.objects.get()
        self.assertEqual(row.email, "bounced@example.de")
        self.assertEqual(row.reason, SuppressionReason.HARD_BOUNCE)

    def test_a_complaint_suppresses(self):
        self.post("spam")
        self.assertEqual(
            SuppressedAddress.objects.get().reason, SuppressionReason.COMPLAINT
        )

    def test_a_blocked_event_suppresses(self):
        self.post("blocked")
        self.assertEqual(
            SuppressedAddress.objects.get().reason, SuppressionReason.BLOCKED
        )

    def test_a_soft_bounce_does_not_suppress(self):
        # A full mailbox or a greylisting provider is exactly the situation
        # spec §4 says German providers put us in. Suppressing it would lock a
        # real user out of their own account permanently.
        self.post("soft_bounce")
        self.assertEqual(SuppressedAddress.objects.count(), 0)

    def test_a_delivery_does_not_suppress(self):
        self.post("delivered")
        self.assertEqual(SuppressedAddress.objects.count(), 0)

    def test_an_open_does_not_suppress(self):
        self.post("unique_opened")
        self.assertEqual(SuppressedAddress.objects.count(), 0)

    def test_repeated_events_are_idempotent(self):
        self.post("hard_bounce")
        self.post("hard_bounce")
        self.assertEqual(SuppressedAddress.objects.count(), 1)


class ClassificationTests(TestCase):
    """The decision function, isolated from HTTP."""

    class FakeEvent:
        def __init__(self, esp_event=None, event_type=None):
            self.esp_event = esp_event
            self.event_type = event_type

    def test_the_raw_event_name_wins(self):
        self.assertEqual(
            suppression_for(self.FakeEvent({"event": "hard_bounce"})),
            SuppressionReason.HARD_BOUNCE,
        )
        self.assertIsNone(suppression_for(self.FakeEvent({"event": "soft_bounce"})))

    def test_an_ambiguous_bounce_without_a_raw_name_is_not_suppressed(self):
        from anymail.signals import EventType

        # BOUNCED covers both hard and soft, and guessing wrong is a lockout.
        self.assertIsNone(
            suppression_for(self.FakeEvent(None, EventType.BOUNCED))
        )

    def test_a_complaint_without_a_raw_name_is_still_suppressed(self):
        from anymail.signals import EventType

        self.assertEqual(
            suppression_for(self.FakeEvent(None, EventType.COMPLAINED)),
            SuppressionReason.COMPLAINT,
        )

    def test_an_unknown_event_is_ignored(self):
        self.assertIsNone(suppression_for(self.FakeEvent({"event": "banana"})))
