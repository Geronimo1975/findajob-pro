"""``manage.py seed_legal`` — idempotent, stable, and tamper-evident."""

from __future__ import annotations

from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.legal.hashing import compute_content_hash
from apps.legal.models import DOC_ID_ORDER, LANG_ORDER, LegalDocument
from tests.legal.support import LEGAL_JS, seed, shipped_date, shipped_version


class SeedLegalTests(TestCase):
    def test_seeds_six_documents_in_three_languages(self):
        seed()
        self.assertEqual(LegalDocument.objects.count(), 6 * 3)
        for doc_id in DOC_ID_ORDER:
            for lang in LANG_ORDER:
                with self.subTest(doc=doc_id, lang=lang):
                    self.assertTrue(
                        LegalDocument.objects.filter(
                            doc_id=doc_id, lang=lang, is_current=True
                        ).exists()
                    )

    def test_every_seeded_row_is_current_and_hashes_its_own_text(self):
        seed()
        for document in LegalDocument.objects.all():
            with self.subTest(document=str(document)):
                self.assertTrue(document.is_current)
                self.assertTrue(document.hash_matches_content)
                self.assertEqual(len(document.content_hash), 64)
                self.assertTrue(document.sections)

    def test_is_idempotent(self):
        seed()
        before = {
            (doc.doc_id, doc.lang): (doc.pk, doc.version, doc.content_hash)
            for doc in LegalDocument.objects.all()
        }
        seed()
        after = {
            (doc.doc_id, doc.lang): (doc.pk, doc.version, doc.content_hash)
            for doc in LegalDocument.objects.all()
        }
        self.assertEqual(before, after)
        self.assertEqual(LegalDocument.objects.count(), 18)

    def test_second_run_reports_everything_as_unchanged(self):
        seed()
        out = StringIO()
        call_command("seed_legal", path=str(LEGAL_JS), stdout=out, verbosity=1)
        self.assertIn("0 created", out.getvalue())
        self.assertIn("18 unchanged", out.getvalue())

    def test_version_is_derived_from_the_document_date(self):
        seed()
        for doc_id in DOC_ID_ORDER:
            for lang in LANG_ORDER:
                with self.subTest(doc=doc_id, lang=lang):
                    document = LegalDocument.objects.get(
                        doc_id=doc_id, lang=lang, is_current=True
                    )
                    self.assertEqual(
                        document.version, shipped_version(doc_id, lang)
                    )

    def test_all_languages_of_a_document_share_one_version(self):
        """Otherwise the German-only staleness check 409s every EN/RO visitor."""
        seed()
        for doc_id in DOC_ID_ORDER:
            with self.subTest(doc=doc_id):
                versions = set(
                    LegalDocument.objects.filter(doc_id=doc_id).values_list(
                        "version", flat=True
                    )
                )
                self.assertEqual(len(versions), 1)

    def test_languages_of_one_document_have_different_hashes(self):
        """The hash covers the language, so translations are distinguishable."""
        seed()
        hashes = set(
            LegalDocument.objects.filter(doc_id="terms").values_list(
                "content_hash", flat=True
            )
        )
        self.assertEqual(len(hashes), 3)

    def test_note_is_stored_per_language(self):
        seed()
        german = LegalDocument.objects.get(doc_id="terms", lang="de")
        english = LegalDocument.objects.get(doc_id="terms", lang="en")
        self.assertIn("maßgeblich", german.note)
        self.assertIn("binding", english.note)

    def test_note_is_not_part_of_the_hash(self):
        """A wording tweak to the translation notice must not invalidate 18 hashes."""
        seed()
        document = LegalDocument.objects.get(doc_id="terms", lang="de")
        recomputed = compute_content_hash(
            doc_id=document.doc_id,
            lang=document.lang,
            version=document.version,
            title=document.title,
            sections=document.sections,
        )
        self.assertEqual(document.content_hash, recomputed)

    def test_dry_run_writes_nothing(self):
        out = StringIO()
        call_command(
            "seed_legal", path=str(LEGAL_JS), dry_run=True, stdout=out, verbosity=1
        )
        self.assertEqual(LegalDocument.objects.count(), 0)
        self.assertIn("Dry run", out.getvalue())

    def test_single_language_seeding(self):
        seed(lang=["de"])
        self.assertEqual(LegalDocument.objects.count(), 6)
        self.assertEqual(
            set(LegalDocument.objects.values_list("lang", flat=True)), {"de"}
        )

    def test_unknown_language_is_an_error(self):
        with self.assertRaises(CommandError):
            seed(lang=["fr"])

    def test_missing_file_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command("seed_legal", path="/nonexistent/legal.js", verbosity=0)


class SeedDriftTests(TestCase):
    """Text that changes without a date bump must not silently rewrite a row."""

    def setUp(self):
        seed()
        self.document = LegalDocument.objects.get(doc_id="terms", lang="de")

    def _tamper(self) -> None:
        LegalDocument.objects.filter(pk=self.document.pk).update(
            content_hash="0" * 64
        )

    def test_drifted_version_is_refused(self):
        self._tamper()
        with self.assertRaises(CommandError) as ctx:
            seed()
        message = str(ctx.exception)
        self.assertIn("terms/de", message)
        self.assertIn(shipped_version("terms", "de"), message)

    def test_a_refused_run_writes_nothing_at_all(self):
        """The whole seed is one transaction: a drift aborts every row."""
        self._tamper()
        LegalDocument.objects.filter(doc_id="privacy", lang="ro").delete()
        with self.assertRaises(CommandError):
            seed()
        self.assertFalse(
            LegalDocument.objects.filter(doc_id="privacy", lang="ro").exists()
        )

    def test_force_overwrites_the_drifted_row(self):
        self._tamper()
        seed(force=True)
        self.document.refresh_from_db()
        self.assertTrue(self.document.hash_matches_content)


class SeedCurrentPointerTests(TestCase):
    def test_a_new_version_demotes_the_old_one(self):
        seed(doc_version="2026-07-01")
        seed(doc_version="2026-08-01")
        rows = LegalDocument.objects.filter(doc_id="terms", lang="de").order_by(
            "version"
        )
        self.assertEqual([row.version for row in rows], ["2026-07-01", "2026-08-01"])
        self.assertEqual([row.is_current for row in rows], [False, True])

    def test_exactly_one_current_row_per_document_and_language(self):
        seed(doc_version="2026-07-01")
        seed(doc_version="2026-08-01")
        for doc_id in DOC_ID_ORDER:
            for lang in LANG_ORDER:
                with self.subTest(doc=doc_id, lang=lang):
                    self.assertEqual(
                        LegalDocument.objects.filter(
                            doc_id=doc_id, lang=lang, is_current=True
                        ).count(),
                        1,
                    )

    def test_effective_from_still_comes_from_the_document_date(self):
        """``--version`` overrides the label, not the date the text took effect."""
        seed(doc_version="hotfix-1")
        document = LegalDocument.objects.get(doc_id="terms", lang="de")
        self.assertEqual(document.version, "hotfix-1")
        # Compared against the *date*, not against shipped_version(): the two
        # happen to render identically today, so using the version here would
        # pass for a reason that has nothing to do with effective_from the
        # moment anyone prefixes versions ("v2026-08-02").
        self.assertEqual(document.effective_from.date(), shipped_date("terms", "de"))


class ShippedCorpusVersionTests(TestCase):
    """The external oracle for the shipped contract versions.

    Every other version assertion in this package goes through
    ``shipped_version()``, which computes the expected value with the same
    parser and date reader the seeder uses. That kills a class of brittleness —
    a literal that has to be edited whenever any unrelated document is
    re-dated — but it is circular: a mutation of ``version_for_date`` or
    ``parse_document_date`` leaves all of them green.

    These two literals are the fix. They are written by hand, and they are
    *supposed* to need a human edit: a legal document changing version is a
    contractual event, and the person doing it should have to say so in the
    diff. Re-deriving them from ``web/legal.js`` would defeat the entire point
    of this class — don't.

    When they fail, the question to ask is not "what is the new date" but
    "was this document meant to be re-versioned, and has prod been re-seeded?"
    """

    def test_the_binding_terms_version_is_what_we_think_it_is(self):
        self.assertEqual(shipped_version("terms", "de"), "2026-08-02")
        self.assertEqual(shipped_version("dpa", "de"), "2026-08-02")

    def test_the_untouched_documents_are_still_on_their_original_version(self):
        for doc_id in ("privacy", "cookies", "impressum", "withdrawal"):
            with self.subTest(doc=doc_id):
                self.assertEqual(shipped_version(doc_id, "de"), "2026-08-01")
