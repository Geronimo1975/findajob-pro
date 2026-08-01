"""Turning ``legal.js``'s human date strings into a machine version.

``web/legal.js`` carries no version field. What it has is an ``updated`` string,
written the way each language writes dates::

    en: 'Aug 1, 2026'
    de: '1. Aug. 2026'
    ro: '1 aug. 2026'

The version is that date in ISO form (``2026-08-01``), which gives three
properties the consent ledger needs:

* **Identical across languages.** The staleness check in
  :func:`apps.legal.services.record_registration_consent` compares the client's
  string against the *German* document's version, while the client may well have
  been reading the English one. If EN and DE produced different version strings,
  every English visitor would get a spurious ``409 terms_outdated``. The seed
  command warns loudly when a document's languages disagree on the date.
* **Sortable and human-obvious**, unlike a hash prefix.
* **Stable under re-seeding** — the same file always yields the same version,
  which is what makes ``seed_legal`` idempotent.

Parsing is deliberately permissive about separators and case, and deliberately
strict about the result: an unparseable date raises rather than defaulting to
"today", because a version that changes every time the seeder runs would mint a
new document version on every deploy and invalidate every consent record.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone

__all__ = ["UnparseableDate", "parse_document_date", "version_for_date", "effective_from_for_date"]


class UnparseableDate(ValueError):
    """Raised when a document's ``updated`` string is not a date we can read."""


def _fold(token: str) -> str:
    """Lowercase, drop accents and trailing punctuation: ``'Mär.'`` -> ``'mar'``."""
    text = unicodedata.normalize("NFKD", token).lower()
    text = "".join(char for char in text if not unicodedata.combining(char))
    return text.strip(" .,")


#: Month names and abbreviations in the three shipped languages. Folded through
#: :func:`_fold`, so ``'mär'``/``'mrz'``/``'mar'`` all land on March and the
#: Romanian ``'ian'`` on January.
_MONTHS: dict[str, int] = {}


def _register(number: int, *names: str) -> None:
    for name in names:
        _MONTHS[_fold(name)] = number


_register(1, "jan", "january", "januar", "ian", "ianuarie")
_register(2, "feb", "february", "februar", "februarie")
_register(3, "mar", "march", "mär", "märz", "mrz", "martie")
_register(4, "apr", "april", "aprilie")
_register(5, "may", "mai", "mei")
_register(6, "jun", "june", "juni", "iun", "iunie")
_register(7, "jul", "july", "juli", "iul", "iulie")
_register(8, "aug", "august", "aoû")
_register(9, "sep", "sept", "september", "septembrie")
_register(10, "oct", "october", "okt", "oktober", "octombrie")
_register(11, "nov", "november", "noi", "noiembrie")
_register(12, "dec", "december", "dez", "dezember", "decembrie")

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_SPLIT_RE = re.compile(r"[\s.,/]+")


def parse_document_date(value: str) -> date:
    """Parse an ``updated`` string from ``legal.js`` into a :class:`date`.

    Understands ISO (``2026-08-01``) and the three shipped human formats, in any
    order of day / month-name / year. Raises :class:`UnparseableDate` on
    anything else — including a purely numeric ``01/08/2026``, which is
    genuinely ambiguous between the American and European reading and must be
    disambiguated by a human, not guessed here.
    """
    text = (value or "").strip()
    if not text:
        raise UnparseableDate("The document has no `updated` date.")

    iso = _ISO_RE.match(text)
    if iso:
        year, month, day = (int(part) for part in iso.groups())
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise UnparseableDate(f"{text!r} is not a real date: {exc}") from None

    tokens = [token for token in _SPLIT_RE.split(text) if token]
    month = day = year = None
    for token in tokens:
        folded = _fold(token)
        if folded in _MONTHS and month is None:
            month = _MONTHS[folded]
            continue
        if folded.isdigit():
            number = int(folded)
            if len(folded) == 4 and year is None:
                year = number
            elif 1 <= number <= 31 and day is None:
                day = number
            elif year is None and number >= 1000:
                year = number
            else:
                raise UnparseableDate(
                    f"Could not place the number {token!r} in {text!r}."
                )
            continue
        raise UnparseableDate(f"Unrecognised token {token!r} in {text!r}.")

    if month is None or day is None or year is None:
        raise UnparseableDate(
            f"{text!r} does not contain a day, a month name and a four-digit year."
        )
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise UnparseableDate(f"{text!r} is not a real date: {exc}") from None


def version_for_date(value: date) -> str:
    """The version string for a document date — ISO, e.g. ``2026-08-01``."""
    return value.isoformat()


def effective_from_for_date(value: date) -> datetime:
    """Midnight UTC on the document's date, timezone-aware.

    ``django.utils.timezone.utc`` was removed in Django 5.0; this uses
    ``datetime.timezone.utc``, which is the supported spelling.
    """
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
