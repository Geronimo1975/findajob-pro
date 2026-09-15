"""``POST /api/v1/leads/demo/`` — the B2B demo-request form."""

from __future__ import annotations

from django.core import mail
from django.test import override_settings

from tests.accounts.support import AuthTestCase, csrf_client

URL = "/api/v1/leads/demo/"
LEAD = {"company": "Acme Coaching", "email": "hr@acme.de", "segment": "coaching", "message": "10 seats"}


@override_settings(ADMINS=["ops@findajob.pro"])
class DemoLeadTests(AuthTestCase):
    def test_mails_admins_and_returns_202(self):
        response = self.post_json(URL, LEAD)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["ops@findajob.pro"])
        self.assertIn("Acme Coaching", mail.outbox[0].body)

    def test_honeypot_is_silently_accepted(self):
        response = self.post_json(URL, {**LEAD, "website": "http://spam"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(mail.outbox, [])

    def test_invalid_segment_is_400(self):
        response = self.post_json(URL, {**LEAD, "segment": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid")

    @override_settings(ADMINS=[])
    def test_no_admins_is_503_not_a_lost_lead(self):
        response = self.post_json(URL, LEAD)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "delivery_unavailable")

    def test_csrf_is_enforced(self):
        client, _token = csrf_client()
        response = client.post(URL, LEAD, content_type="application/json")
        self.assertEqual(response.status_code, 403)
