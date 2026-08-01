"""Model-level invariants: one current version, and an append-only ledger."""

from __future__ import annotations

from datetime import datetime, timezone

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.legal.models import ConsentRecord, ConsentType, LegalDocument
from tests.legal.support import make_document
from tests.support import make_user


class LegalDocumentConstraintTests(TestCase):
    def test_only_one_current_version_per_doc_and_language(self):
        make_document(doc_id="terms", lang="de", version="2026-01-01", is_current=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_document(
                    doc_id="terms", lang="de", version="2026-02-01", is_current=True
                )

    def test_a_superseded_version_may_coexist(self):
        make_document(doc_id="terms", lang="de", version="2026-01-01", is_current=False)
        make_document(doc_id="terms", lang="de", version="2026-02-01", is_current=True)
        self.assertEqual(
            LegalDocument.objects.filter(doc_id="terms", lang="de").count(), 2
        )

    def test_current_is_per_language(self):
        make_document(doc_id="terms", lang="de", is_current=True)
        make_document(doc_id="terms", lang="en", is_current=True)
        self.assertEqual(LegalDocument.objects.current().count(), 2)

    def test_doc_lang_version_is_unique(self):
        make_document(doc_id="terms", lang="de", version="2026-01-01", is_current=True)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_document(
                    doc_id="terms", lang="de", version="2026-01-01", is_current=False
                )

    def test_blank_hash_is_refused(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                LegalDocument.objects.create(
                    doc_id="terms",
                    lang="de",
                    version="2026-01-01",
                    title="AGB",
                    sections=[],
                    content_hash="",
                    effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                )

    def test_hash_matches_content_detects_a_hand_edit(self):
        document = make_document()
        self.assertTrue(document.hash_matches_content)
        document.sections = [{"heading": "1", "body": "Tampered"}]
        self.assertFalse(document.hash_matches_content)


class ConsentRecordAppendOnlyTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def _record(self, **overrides) -> ConsentRecord:
        kwargs = {
            "user": self.user,
            "type": ConsentType.TERMS.value,
            "granted": True,
            "doc_version": "2026-08-01",
            "content_hash": "a" * 64,
            "doc_lang": "de",
        }
        kwargs.update(overrides)
        return ConsentRecord.objects.create(**kwargs)

    def test_a_saved_record_cannot_be_modified(self):
        record = self._record()
        record.granted = False
        with self.assertRaises(ValueError) as ctx:
            record.save()
        self.assertIn("append-only", str(ctx.exception))

    def test_the_row_is_unchanged_after_a_refused_update(self):
        record = self._record()
        record.granted = False
        with self.assertRaises(ValueError):
            record.save()
        record.refresh_from_db()
        self.assertTrue(record.granted)

    def test_force_update_is_refused(self):
        record = self._record()
        with self.assertRaises(ValueError):
            record.save(force_update=True)

    def test_reloading_and_saving_is_still_refused(self):
        """``refresh_from_db`` gives a clean instance — still no updates."""
        record = self._record()
        reloaded = ConsentRecord.objects.get(pk=record.pk)
        reloaded.ui_language = "en"
        with self.assertRaises(ValueError):
            reloaded.save()

    def test_revocation_is_a_new_row_not_an_edit(self):
        self._record(type=ConsentType.SEND_ON_BEHALF.value, granted=True)
        self._record(type=ConsentType.SEND_ON_BEHALF.value, granted=False)
        rows = ConsentRecord.objects.for_user(self.user).of_type(
            ConsentType.SEND_ON_BEHALF.value
        )
        self.assertEqual(rows.count(), 2)

    def test_latest_per_type_uses_id_as_the_tiebreak(self):
        """Rows written in one transaction can share created_at to the µs."""
        self._record(type=ConsentType.MARKETING.value, granted=True)
        newest = self._record(type=ConsentType.MARKETING.value, granted=False)
        latest = ConsentRecord.objects.latest_per_type(self.user)
        self.assertEqual(latest[ConsentType.MARKETING.value].pk, newest.pk)
        self.assertFalse(latest[ConsentType.MARKETING.value].granted)

    def test_deleting_the_user_erases_their_records(self):
        """Art. 17 erasure must still work — append-only is not undeletable."""
        self._record()
        self.user.delete()
        self.assertEqual(ConsentRecord.objects.count(), 0)
