"""The legal + consent HTTP surface."""

from __future__ import annotations

import json

from django.test import Client, TestCase
from django.urls import reverse

from apps.legal.models import ConsentRecord, ConsentType, LegalDocument
from tests.legal.support import csrf_pair, seed
from tests.support import make_user


class LegalReadEndpointTests(TestCase):
    """``legal/*`` is one of the three anonymous surfaces spec §7 allows."""

    @classmethod
    def setUpTestData(cls):
        seed()

    def test_index_is_public(self):
        response = self.client.get("/api/v1/legal/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["documents"]), 6)
        self.assertEqual(body["binding_lang"], "de")
        self.assertEqual(body["languages"], ["en", "de", "ro"])

    def test_index_entries_carry_no_body_text(self):
        body = self.client.get("/api/v1/legal/").json()
        self.assertNotIn("sections", body["documents"][0])
        self.assertIn("content_hash", body["documents"][0])

    def test_document_is_public_and_has_sections(self):
        response = self.client.get("/api/v1/legal/terms/?lang=de")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["doc_id"], "terms")
        self.assertEqual(body["lang"], "de")
        self.assertEqual(body["version"], "2026-08-01")
        self.assertTrue(body["is_binding"])
        self.assertTrue(body["sections"])
        self.assertIn("heading", body["sections"][0])
        self.assertIn("body", body["sections"][0])

    def test_language_selection(self):
        for lang in ("en", "de", "ro"):
            with self.subTest(lang=lang):
                body = self.client.get(f"/api/v1/legal/terms/?lang={lang}").json()
                self.assertEqual(body["lang"], lang)
                self.assertEqual(body["requested_lang"], lang)

    def test_unsupported_language_falls_back_to_the_default(self):
        body = self.client.get("/api/v1/legal/terms/?lang=fr").json()
        self.assertEqual(body["requested_lang"], "en")

    def test_missing_translation_falls_back_to_the_binding_text(self):
        LegalDocument.objects.filter(doc_id="terms", lang="ro").delete()
        body = self.client.get("/api/v1/legal/terms/?lang=ro").json()
        self.assertEqual(body["lang"], "de")
        self.assertEqual(body["requested_lang"], "ro")

    def test_unknown_document_is_a_404_envelope(self):
        response = self.client.get("/api/v1/legal/secret/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "not_found")

    def test_path_traversal_does_not_resolve(self):
        self.assertEqual(self.client.get("/api/v1/legal/../../etc/passwd/").status_code, 404)

    def test_unseeded_document_is_a_404(self):
        LegalDocument.objects.filter(doc_id="dpa").delete()
        self.assertEqual(self.client.get("/api/v1/legal/dpa/").status_code, 404)
        body = self.client.get("/api/v1/legal/").json()
        self.assertEqual(len(body["documents"]), 5)

    def test_reverse_matches_the_pinned_paths(self):
        self.assertEqual(reverse("legal:index"), "/api/v1/legal/")
        self.assertEqual(
            reverse("legal:document", kwargs={"doc_id": "privacy"}),
            "/api/v1/legal/privacy/",
        )


class ConsentEndpointAuthTests(TestCase):
    def test_get_requires_a_session(self):
        response = self.client.get("/api/v1/consent/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "not_authenticated")

    def test_post_requires_a_session(self):
        response = self.client.post(
            "/api/v1/consent/",
            data=json.dumps({"type": "marketing", "granted": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_delete_requires_a_session(self):
        self.assertEqual(
            self.client.delete("/api/v1/consent/marketing/").status_code, 401
        )


class ConsentEndpointTests(TestCase):
    def setUp(self):
        seed()
        self.user = make_user()
        self.client.force_login(self.user)

    def post(self, payload):
        return self.client.post(
            "/api/v1/consent/",
            data=json.dumps(payload),
            content_type="application/json",
        )

    def test_get_reports_current_state(self):
        body = self.client.get("/api/v1/consent/").json()
        self.assertEqual(body["consents"], {})
        self.assertEqual(body["granted"], [])
        self.assertIn("send_on_behalf", body["revocable"])

    def test_post_grants_and_returns_the_record(self):
        response = self.post({"type": "send_on_behalf", "granted": True})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["type"], "send_on_behalf")
        self.assertTrue(body["granted"])
        self.assertEqual(len(body["content_hash"]), 64)

    def test_post_records_the_ui_language(self):
        self.post({"type": "marketing", "granted": True, "lang": "ro"})
        record = ConsentRecord.objects.for_user(self.user).of_type("marketing").get()
        self.assertEqual(record.ui_language, "ro")

    def test_delete_revokes_without_deleting_anything(self):
        self.post({"type": "send_on_behalf", "granted": True})
        response = self.client.delete("/api/v1/consent/send_on_behalf/")
        self.assertEqual(response.status_code, 204)
        rows = ConsentRecord.objects.for_user(self.user).of_type("send_on_behalf")
        self.assertEqual(rows.count(), 2)
        self.assertFalse(rows.order_by("-id").first().granted)
        self.assertEqual(self.client.get("/api/v1/consent/").json()["granted"], [])

    def test_terms_cannot_be_posted(self):
        response = self.post({"type": "terms", "granted": False})
        self.assertEqual(response.status_code, 400)

    def test_terms_cannot_be_deleted(self):
        response = self.client.delete("/api/v1/consent/terms/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid")

    def test_unknown_type_is_rejected(self):
        self.assertEqual(self.post({"type": "telepathy", "granted": True}).status_code, 400)
        self.assertEqual(self.client.delete("/api/v1/consent/telepathy/").status_code, 400)

    def test_the_response_never_echoes_ip_or_user_agent(self):
        body = self.post({"type": "marketing", "granted": True}).json()
        self.assertNotIn("ip", body)
        self.assertNotIn("user_agent", body)

    def test_history_only_ever_shows_the_caller_s_own_records(self):
        other = make_user()
        ConsentRecord.objects.create(
            user=other, type=ConsentType.MARKETING.value, granted=True
        )
        self.post({"type": "send_on_behalf", "granted": True})
        body = self.client.get("/api/v1/consent/").json()
        self.assertEqual([row["type"] for row in body["history"]], ["send_on_behalf"])

    def test_ip_is_recorded_from_remote_addr(self):
        self.client.post(
            "/api/v1/consent/",
            data=json.dumps({"type": "marketing", "granted": True}),
            content_type="application/json",
            REMOTE_ADDR="198.51.100.7",
        )
        record = ConsentRecord.objects.for_user(self.user).of_type("marketing").get()
        self.assertEqual(record.ip, "198.51.100.7")


class ConsentCsrfTests(TestCase):
    """Spec §8: DRF's APIView is csrf_exempt, so the decorator is load-bearing."""

    def setUp(self):
        seed()
        self.user = make_user()
        self.client = Client(enforce_csrf_checks=True)
        self.client.force_login(self.user)

    def test_post_without_a_token_is_rejected(self):
        response = self.client.post(
            "/api/v1/consent/",
            data=json.dumps({"type": "marketing", "granted": True}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")
        self.assertEqual(ConsentRecord.objects.count(), 0)

    def test_delete_without_a_token_is_rejected(self):
        self.assertEqual(
            self.client.delete("/api/v1/consent/marketing/").status_code, 403
        )

    def test_post_with_a_token_succeeds(self):
        secret, token = csrf_pair()
        self.client.cookies["csrftoken"] = secret
        response = self.client.post(
            "/api/v1/consent/",
            data=json.dumps({"type": "marketing", "granted": True}),
            content_type="application/json",
            headers={"x-csrftoken": token},
        )
        self.assertEqual(response.status_code, 201)

    def test_a_get_is_not_blocked(self):
        self.assertEqual(self.client.get("/api/v1/consent/").status_code, 200)
