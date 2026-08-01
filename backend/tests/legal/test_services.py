"""Consent services — spec §10's "server-resolved, never client-supplied"."""

from __future__ import annotations

from django.test import TestCase

from apps.legal.models import (
    BINDING_LANGUAGE,
    ConsentRecord,
    ConsentType,
    LegalDocument,
)
from apps.legal.services import (
    ConsentNotRevocable,
    LegalDocumentUnavailable,
    TermsOutdated,
    consent_history,
    current_consent,
    current_terms_version,
    grant_consent,
    has_consent,
    record_registration_consent,
    resolve_document,
    revoke_consent,
)
from tests.legal.support import make_document, seed
from tests.support import make_user


class RegistrationConsentTests(TestCase):
    def setUp(self):
        seed()
        self.user = make_user()

    def test_writes_terms_and_privacy(self):
        records = record_registration_consent(self.user, ui_language="en")
        self.assertEqual(
            sorted(record.type for record in records),
            [ConsentType.PRIVACY.value, ConsentType.TERMS.value],
        )
        self.assertTrue(all(record.granted for record in records))

    def test_resolves_the_german_document_even_for_an_english_ui(self):
        """The DE text is binding; its hash is the only defensible record."""
        record_registration_consent(self.user, ui_language="en")
        german = LegalDocument.objects.get(
            doc_id="terms", lang=BINDING_LANGUAGE, is_current=True
        )
        english = LegalDocument.objects.get(doc_id="terms", lang="en", is_current=True)
        record = ConsentRecord.objects.for_user(self.user).of_type("terms").get()
        self.assertEqual(record.content_hash, german.content_hash)
        self.assertEqual(record.doc_lang, BINDING_LANGUAGE)
        self.assertNotEqual(record.content_hash, english.content_hash)
        # …and the UI language the user was reading is still recorded.
        self.assertEqual(record.ui_language, "en")

    def test_stores_the_server_version_not_the_client_string(self):
        record_registration_consent(self.user, client_terms_version="2026-08-01")
        record = ConsentRecord.objects.for_user(self.user).of_type("terms").get()
        self.assertEqual(record.doc_version, current_terms_version())

    def test_a_stale_client_version_raises_terms_outdated(self):
        with self.assertRaises(TermsOutdated) as ctx:
            record_registration_consent(self.user, client_terms_version="2020-01-01")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.error_code, "terms_outdated")

    def test_a_stale_client_version_writes_nothing(self):
        with self.assertRaises(TermsOutdated):
            record_registration_consent(self.user, client_terms_version="2020-01-01")
        self.assertEqual(ConsentRecord.objects.count(), 0)

    def test_a_missing_client_version_skips_the_check(self):
        record_registration_consent(self.user, client_terms_version=None)
        record_registration_consent(make_user(), client_terms_version="")
        self.assertEqual(ConsentRecord.objects.count(), 4)

    def test_is_idempotent_for_the_same_version(self):
        record_registration_consent(self.user)
        second = record_registration_consent(self.user)
        self.assertEqual(second, [])
        self.assertEqual(ConsentRecord.objects.for_user(self.user).count(), 2)

    def test_a_new_terms_version_is_consented_again(self):
        record_registration_consent(self.user)
        LegalDocument.objects.filter(doc_id="terms", lang="de").update(is_current=False)
        make_document(doc_id="terms", lang="de", version="2027-01-01", is_current=True)
        record_registration_consent(self.user)
        rows = ConsentRecord.objects.for_user(self.user).of_type("terms")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(rows.order_by("-id").first().doc_version, "2027-01-01")

    def test_ip_and_user_agent_are_recorded(self):
        record_registration_consent(
            self.user, ip="203.0.113.9", user_agent="Mozilla/5.0", ui_language="de"
        )
        record = ConsentRecord.objects.for_user(self.user).of_type("terms").get()
        self.assertEqual(record.ip, "203.0.113.9")
        self.assertEqual(record.user_agent, "Mozilla/5.0")

    def test_nul_bytes_in_the_user_agent_are_stripped(self):
        """A NUL passes Django validation and then 500s the Postgres insert."""
        record_registration_consent(self.user, user_agent="Mozilla\x005.0")
        record = ConsentRecord.objects.for_user(self.user).of_type("terms").get()
        self.assertNotIn("\x00", record.user_agent)


class UnseededDatabaseTests(TestCase):
    """Fail closed: no binding document means no consent record."""

    def test_registration_consent_raises_when_nothing_is_seeded(self):
        user = make_user()
        with self.assertRaises(LegalDocumentUnavailable) as ctx:
            record_registration_consent(user)
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ConsentRecord.objects.count(), 0)

    def test_raises_when_only_the_english_terms_exist(self):
        """An English translation is not a substitute for the binding text."""
        make_document(doc_id="terms", lang="en", is_current=True)
        make_document(doc_id="privacy", lang="en", is_current=True)
        with self.assertRaises(LegalDocumentUnavailable):
            record_registration_consent(make_user())

    def test_current_terms_version_is_empty_rather_than_raising(self):
        self.assertEqual(current_terms_version(), "")


class ResolveDocumentTests(TestCase):
    def setUp(self):
        seed()

    def test_returns_the_requested_language(self):
        document, lang = resolve_document("terms", "ro")
        self.assertEqual(lang, "ro")
        self.assertEqual(document.lang, "ro")

    def test_falls_back_to_the_binding_language(self):
        LegalDocument.objects.filter(doc_id="terms", lang="ro").delete()
        document, lang = resolve_document("terms", "ro")
        self.assertEqual(lang, BINDING_LANGUAGE)
        self.assertEqual(document.lang, BINDING_LANGUAGE)

    def test_returns_none_for_an_unknown_document(self):
        document, _lang = resolve_document("nope", "de")
        self.assertIsNone(document)

    def test_ignores_a_superseded_version(self):
        LegalDocument.objects.filter(doc_id="terms", lang="de").update(is_current=False)
        newer = make_document(doc_id="terms", lang="de", version="2027-02-02")
        document, _lang = resolve_document("terms", "de")
        self.assertEqual(document.pk, newer.pk)


class OptInConsentTests(TestCase):
    def setUp(self):
        seed()
        self.user = make_user()

    def test_grant_and_revoke_append_rows(self):
        grant_consent(self.user, ConsentType.SEND_ON_BEHALF.value)
        self.assertTrue(has_consent(self.user, ConsentType.SEND_ON_BEHALF.value))
        revoke_consent(self.user, ConsentType.SEND_ON_BEHALF.value)
        self.assertFalse(has_consent(self.user, ConsentType.SEND_ON_BEHALF.value))
        self.assertEqual(ConsentRecord.objects.for_user(self.user).count(), 2)

    def test_absence_of_a_record_is_absence_of_consent(self):
        self.assertFalse(has_consent(self.user, ConsentType.SEND_ON_BEHALF.value))

    def test_opt_in_is_evidenced_against_the_mapped_document(self):
        record = grant_consent(self.user, ConsentType.ANALYTICS.value)
        cookies = LegalDocument.objects.get(doc_id="cookies", lang="de", is_current=True)
        self.assertEqual(record.content_hash, cookies.content_hash)

    def test_terms_cannot_be_revoked_through_the_consent_api(self):
        with self.assertRaises(ConsentNotRevocable):
            revoke_consent(self.user, ConsentType.TERMS.value)

    def test_unknown_type_is_rejected(self):
        with self.assertRaises(Exception) as ctx:
            grant_consent(self.user, "telepathy")
        self.assertEqual(getattr(ctx.exception, "status_code", None), 400)

    def test_opt_in_still_works_when_the_cookie_policy_is_missing(self):
        LegalDocument.objects.filter(doc_id="cookies").delete()
        record = grant_consent(self.user, ConsentType.ANALYTICS.value)
        self.assertEqual(record.content_hash, "")
        self.assertTrue(record.granted)

    def test_current_consent_returns_the_newest_decision_per_type(self):
        record_registration_consent(self.user)
        grant_consent(self.user, ConsentType.MARKETING.value)
        revoke_consent(self.user, ConsentType.MARKETING.value)
        latest = current_consent(self.user)
        self.assertTrue(latest[ConsentType.TERMS.value].granted)
        self.assertFalse(latest[ConsentType.MARKETING.value].granted)

    def test_history_is_newest_first_and_bounded(self):
        for _ in range(3):
            grant_consent(self.user, ConsentType.MARKETING.value)
        rows = list(consent_history(self.user, limit=2))
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[0].pk, rows[1].pk)
