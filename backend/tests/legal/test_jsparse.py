"""The JS object-literal reader that makes ``web/legal.js`` seedable."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.legal.jsparse import JsLiteralError, parse_js_literal, parse_module_exports
from tests.legal.support import LEGAL_JS


class ParseJsLiteralTests(SimpleTestCase):
    def test_bare_identifier_keys_and_single_quotes(self):
        self.assertEqual(parse_js_literal("{ title: 'AGB' }"), {"title": "AGB"})

    def test_trailing_commas(self):
        self.assertEqual(parse_js_literal("[1, 2, 3,]"), [1, 2, 3])
        self.assertEqual(parse_js_literal("{a: 1,}"), {"a": 1})

    def test_escapes_and_typographic_quotes(self):
        value = parse_js_literal(r"""['Anbieter\nGreuner Brook 1', 'Niedersachsen („wir")']""")
        self.assertEqual(value[0], "Anbieter\nGreuner Brook 1")
        self.assertIn("„wir", value[1])

    def test_comments_are_skipped(self):
        source = "{ /* block */ a: 1, // line\n b: 2 }"
        self.assertEqual(parse_js_literal(source), {"a": 1, "b": 2})

    def test_literals(self):
        self.assertEqual(
            parse_js_literal("{t: true, f: false, n: null, i: -3, x: 0x10, e: 1.5e2}"),
            {"t": True, "f": False, "n": None, "i": -3, "x": 16, "e": 150.0},
        )

    def test_unicode_escapes_including_surrogate_pairs(self):
        self.assertEqual(parse_js_literal(r"'ä\u{1F600}'"), "ä\U0001F600")

    def test_refuses_an_expression(self):
        with self.assertRaises(JsLiteralError):
            parse_js_literal("someFunction()")

    def test_refuses_template_interpolation(self):
        with self.assertRaises(JsLiteralError):
            parse_js_literal("`hello ${name}`")

    def test_no_eval_of_arbitrary_code(self):
        """The reader parses literals; it must never execute the file."""
        with self.assertRaises(JsLiteralError):
            parse_js_literal("(function(){ return 1; })()")


class ParseRealLegalJsTests(SimpleTestCase):
    """The shipped file must stay machine-readable — this is the seed's input."""

    def test_exports_are_found(self):
        source = LEGAL_JS.read_text(encoding="utf-8")
        exports = parse_module_exports(source, names=("LEGAL", "LEGAL_DOCS"))
        self.assertEqual(
            exports["LEGAL_DOCS"],
            ["terms", "privacy", "cookies", "impressum", "withdrawal", "dpa"],
        )
        self.assertEqual(sorted(exports["LEGAL"]), ["de", "en", "ro"])

    def test_every_document_has_a_title_updated_and_sections(self):
        source = LEGAL_JS.read_text(encoding="utf-8")
        exports = parse_module_exports(source, names=("LEGAL", "LEGAL_DOCS"))
        for lang, block in exports["LEGAL"].items():
            for doc_id in exports["LEGAL_DOCS"]:
                with self.subTest(lang=lang, doc=doc_id):
                    document = block[doc_id]
                    self.assertTrue(document["title"])
                    self.assertTrue(document["updated"])
                    self.assertTrue(document["sections"])
                    for section in document["sections"]:
                        self.assertEqual(len(section), 2)

    def test_missing_requested_export_raises(self):
        with self.assertRaises(JsLiteralError):
            parse_module_exports("const OTHER = 1;", names=("LEGAL",))
