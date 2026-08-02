"""Shared helpers for the apps.legal test modules."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.middleware.csrf import get_token
from django.test import RequestFactory

from apps.legal.hashing import compute_content_hash
from apps.legal.jsparse import parse_module_exports
from apps.legal.models import LegalDocument
from apps.legal.versioning import parse_document_date, version_for_date

#: The file nginx actually serves. Seeding in tests reads the *real* document
#: corpus on purpose: a fixture copy would let the served text and the hashed
#: text drift, which is exactly the failure the hash exists to prevent.
LEGAL_JS = Path(settings.REPO_ROOT) / "web" / "legal.js"


def seed(**kwargs) -> None:
    """Run ``manage.py seed_legal`` against the shipped ``web/legal.js``."""
    kwargs.setdefault("path", str(LEGAL_JS))
    kwargs.setdefault("verbosity", 0)
    call_command("seed_legal", **kwargs)


def shipped_date(doc_id: str = "terms", lang: str = "de") -> date:
    """The ``updated`` date of one shipped document, as a real date.

    Read from ``web/legal.js`` rather than written down here. Documents are
    versioned per document, not per corpus — correcting the AGB does not
    re-issue the Impressum — so a test that hard-codes one date breaks the next
    time a single document is edited, which is not what it meant to assert.
    """
    exports = parse_module_exports(LEGAL_JS.read_text(encoding="utf-8"))
    return parse_document_date(exports["LEGAL"][lang][doc_id]["updated"])


def shipped_version(doc_id: str = "terms", lang: str = "de") -> str:
    """The version ``seed_legal`` will mint for one shipped document.

    Derived through the same parser and date reader the seeder uses, so a test
    can only pass if those agree with it.

    **This helper is not an oracle.** It computes the expected value with the
    same code under test, so an assertion built only on it cannot notice a
    wrong version — proven by mutation: breaking ``version_for_date`` leaves
    every call site here green. It removes noise (a literal that has to be
    edited whenever any unrelated document is re-dated), not risk. The external
    oracle lives in ``ShippedCorpusVersionTests`` in test_seed_legal.py, which
    pins the current contract version by hand on purpose.
    """
    return version_for_date(shipped_date(doc_id, lang))


def csrf_pair() -> tuple[str, str]:
    """A matching ``(cookie_secret, header_token)`` pair.

    The frontend gets these from ``GET /api/v1/auth/csrf/`` (apps.accounts).
    Minting them here keeps the legal tests independent of that endpoint while
    still exercising the real ``CsrfViewMiddleware`` comparison.
    """
    request = RequestFactory().get("/")
    token = get_token(request)
    return request.META["CSRF_COOKIE"], token


def make_document(
    *,
    doc_id: str = "terms",
    lang: str = "de",
    version: str = "2026-01-01",
    title: str = "Test",
    sections=None,
    is_current: bool = True,
    note: str = "",
    effective_from: datetime | None = None,
) -> LegalDocument:
    """A minimal, correctly-hashed document row."""
    sections = sections if sections is not None else [{"heading": "1", "body": "Body"}]
    return LegalDocument.objects.create(
        doc_id=doc_id,
        lang=lang,
        version=version,
        title=title,
        sections=sections,
        note=note,
        content_hash=compute_content_hash(
            doc_id=doc_id,
            lang=lang,
            version=version,
            title=title,
            sections=sections,
        ),
        effective_from=effective_from or datetime(2026, 1, 1, tzinfo=timezone.utc),
        is_current=is_current,
    )
