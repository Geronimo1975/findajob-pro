"""The content hash must be stable, and stable for the right reasons."""

from __future__ import annotations

import unicodedata

from django.test import SimpleTestCase

from apps.legal.hashing import (
    canonical_sections,
    compute_content_hash,
    normalise_text,
)

SECTIONS = [
    ["1. Geltungsbereich", "Diese AGB regeln die Nutzung von Findjob.pro."],
    ["2. Leistungen", "Findjob.pro aggregiert Stellenanzeigen."],
]


def digest(**overrides) -> str:
    kwargs = {
        "doc_id": "terms",
        "lang": "de",
        "version": "2026-08-01",
        "title": "Allgemeine Geschäftsbedingungen (AGB)",
        "sections": SECTIONS,
    }
    kwargs.update(overrides)
    return compute_content_hash(**kwargs)


class ContentHashTests(SimpleTestCase):
    def test_is_a_sha256_hex_digest(self):
        value = digest()
        self.assertEqual(len(value), 64)
        self.assertEqual(value, value.lower())
        int(value, 16)  # raises if it is not hex

    def test_is_deterministic_across_calls(self):
        self.assertEqual(digest(), digest())

    def test_unicode_normalisation_form_does_not_change_the_digest(self):
        """``ä`` as U+00E4 and as ``a`` + U+0308 must hash identically.

        The German corpus is full of umlauts and editors disagree about which
        normal form they save. Without NFC folding, re-saving legal.js in a
        different editor would silently invalidate every consent record.
        """
        decomposed = unicodedata.normalize("NFD", "Allgemeine Geschäftsbedingungen (AGB)")
        composed = unicodedata.normalize("NFC", decomposed)
        self.assertNotEqual(decomposed, composed)
        self.assertEqual(digest(title=decomposed), digest(title=composed))

    def test_line_endings_do_not_change_the_digest(self):
        crlf = [["Anbieter", "George Sebastian Cucuiet\r\nGreuner Brook 1"]]
        lf = [["Anbieter", "George Sebastian Cucuiet\nGreuner Brook 1"]]
        self.assertEqual(digest(sections=crlf), digest(sections=lf))

    def test_trailing_whitespace_does_not_change_the_digest(self):
        padded = [[" 1. Geltungsbereich  ", "Diese AGB regeln die Nutzung von Findjob.pro.  "]]
        clean = [[" 1. Geltungsbereich", "Diese AGB regeln die Nutzung von Findjob.pro."]]
        self.assertEqual(digest(sections=padded), digest(sections=clean))

    def test_section_storage_shape_does_not_change_the_digest(self):
        """Pairs (as in legal.js) and dicts (as stored) must agree."""
        as_dicts = [{"heading": head, "body": body} for head, body in SECTIONS]
        self.assertEqual(digest(sections=SECTIONS), digest(sections=as_dicts))

    def test_changing_any_covered_field_changes_the_digest(self):
        base = digest()
        self.assertNotEqual(base, digest(doc_id="privacy"))
        self.assertNotEqual(base, digest(lang="en"))
        self.assertNotEqual(base, digest(version="2026-09-01"))
        self.assertNotEqual(base, digest(title="Andere AGB"))
        self.assertNotEqual(
            base, digest(sections=[["1. Geltungsbereich", "Etwas anderes."]])
        )

    def test_section_order_is_significant(self):
        self.assertNotEqual(digest(), digest(sections=list(reversed(SECTIONS))))

    def test_canonical_sections_rejects_a_one_element_section(self):
        with self.assertRaises(ValueError):
            canonical_sections([["only a heading"]])

    def test_canonical_sections_rejects_a_bare_string(self):
        with self.assertRaises(TypeError):
            canonical_sections(["not a pair"])

    def test_normalise_text_handles_none(self):
        self.assertEqual(normalise_text(None), "")
