"""Version derivation from ``legal.js``'s human date strings."""

from __future__ import annotations

from datetime import date, timezone

from django.test import SimpleTestCase

from apps.legal.versioning import (
    UnparseableDate,
    effective_from_for_date,
    parse_document_date,
    version_for_date,
)


class ParseDocumentDateTests(SimpleTestCase):
    def test_parses_the_three_shipped_formats_to_the_same_day(self):
        """EN / DE / RO must agree, or every non-German visitor gets a 409."""
        for value in ("Aug 1, 2026", "1. Aug. 2026", "1 aug. 2026"):
            with self.subTest(value=value):
                self.assertEqual(parse_document_date(value), date(2026, 8, 1))

    def test_parses_the_day_the_corpus_currently_ships(self):
        """The AGB and the AVV moved to Aug 2; day 1 was the only day covered.

        Kept as its own case on purpose: with only day-1 fixtures, a reader
        that silently collapsed the day-of-month would have passed everything
        here while minting a version that collides with the already-seeded
        2026-08-01 rows in production.
        """
        for value in ("Aug 2, 2026", "2. Aug. 2026", "2 aug. 2026"):
            with self.subTest(value=value):
                self.assertEqual(parse_document_date(value), date(2026, 8, 2))

    def test_parses_iso(self):
        self.assertEqual(parse_document_date("2026-08-01"), date(2026, 8, 1))

    def test_parses_full_month_names_in_each_language(self):
        cases = {
            "August 1, 2026": date(2026, 8, 1),
            "15. Dezember 2026": date(2026, 12, 15),
            "3 ianuarie 2027": date(2027, 1, 3),
            "1. März 2026": date(2026, 3, 1),
            "1 martie 2026": date(2026, 3, 1),
            "5 noiembrie 2026": date(2026, 11, 5),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(parse_document_date(value), expected)

    def test_rejects_an_all_numeric_date(self):
        """``01/08/2026`` is ambiguous between US and EU; guessing is not ok."""
        with self.assertRaises(UnparseableDate):
            parse_document_date("01/08/2026")

    def test_rejects_empty_and_nonsense(self):
        for value in ("", "   ", "soon", "Aug 2026", "32. Aug. 2026"):
            with self.subTest(value=value):
                with self.assertRaises(UnparseableDate):
                    parse_document_date(value)

    def test_version_is_iso(self):
        self.assertEqual(version_for_date(date(2026, 8, 1)), "2026-08-01")

    def test_effective_from_is_timezone_aware_utc_midnight(self):
        moment = effective_from_for_date(date(2026, 8, 1))
        self.assertEqual(moment.tzinfo, timezone.utc)
        self.assertEqual((moment.hour, moment.minute, moment.second), (0, 0, 0))
        self.assertIsNotNone(moment.utcoffset())
