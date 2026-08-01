"""Consent services — the server-side half of implementation spec §10.

    At verify time, inside the same transaction that creates the user:

    - resolve LegalDocument.objects.get(doc_id="terms", lang="de",
      is_current=True) **on the server** — the DE version is the legally binding
      one and its hash is the only defensible record. A client-sent version
      string is used **only** to detect staleness → 409 terms_outdated.
    - write ConsentRecord rows for `terms` and `privacy`.

Read that literally. :func:`record_registration_consent` takes the client's
version string, compares it, and then throws it away: what lands in the ledger
is the version and hash the *server* resolved. A client that lies about the
version it displayed either matches (and is ignored) or does not (and is
rejected); either way it can never author the record.

The other rule this module enforces is fail-closed: if the binding German terms
have not been seeded, registration consent raises instead of writing a record
with an empty hash. An unenforceable consent record is worse than none, because
it looks like evidence.
"""

from __future__ import annotations

import logging
from typing import Iterable

from django.conf import settings
from django.db import transaction
from rest_framework import status

from apps.common.errors import ApiError, ErrorCode
from apps.legal.models import (
    BINDING_LANGUAGE,
    CONSENT_DOCUMENT,
    LANG_FALLBACK_ORDER,
    REGISTRATION_CONSENT_TYPES,
    REVOCABLE_CONSENT_TYPES,
    ConsentRecord,
    ConsentType,
    DocId,
    LegalDocument,
)

logger = logging.getLogger("findajob.legal")

__all__ = [
    "ConsentNotRevocable",
    "LegalDocumentUnavailable",
    "TermsOutdated",
    "current_consent",
    "current_terms_version",
    "grant_consent",
    "record_registration_consent",
    "resolve_document",
    "revoke_consent",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TermsOutdated(ApiError):
    """409 — the client displayed a terms version that is no longer current.

    Spec §3 pins the machine code ``terms_outdated``; ``web/auth.html`` branches
    on it to reload the document and re-ask, rather than showing a dead end.
    """

    status_code = status.HTTP_409_CONFLICT
    error_code = ErrorCode.TERMS_OUTDATED
    default_detail = (
        "The terms have been updated since this page was loaded. "
        "Please reload and accept the current version."
    )


class LegalDocumentUnavailable(ApiError):
    """503 — a document that must exist has not been seeded.

    Deliberately not a 500: nothing crashed, the deployment is incomplete. The
    fix is ``manage.py seed_legal``.
    """

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    error_code = ErrorCode.SERVICE_UNAVAILABLE
    default_detail = (
        "The legal documents are not available right now. Please try again shortly."
    )


class ConsentNotRevocable(ApiError):
    """400 — terms/privacy cannot be withdrawn through the consent API."""

    status_code = status.HTTP_400_BAD_REQUEST
    error_code = ErrorCode.INVALID
    default_detail = (
        "This consent cannot be withdrawn here. Deleting your account withdraws "
        "acceptance of the terms and erases your data."
    )


# ---------------------------------------------------------------------------
# Document resolution
# ---------------------------------------------------------------------------


def _supported_languages() -> tuple[str, ...]:
    return tuple(getattr(settings, "SUPPORTED_LANGUAGE_CODES", ("en", "de", "ro")))


def normalise_lang(value, *, default: str | None = None) -> str:
    """Coerce anything into a supported language code.

    Mirrors ``apps.tenancy.serializers.normalise_lang`` intentionally rather
    than importing it: legal is a public, anonymous surface and must not grow a
    dependency on the tenancy app to answer a GET.
    """
    supported = _supported_languages()
    text = (str(value) if value else "").strip().lower()
    if text in supported:
        return text
    base = text.split("-", 1)[0]
    if base in supported:
        return base
    fallback = default or str(getattr(settings, "LANGUAGE_CODE", "en")).lower()
    return fallback if fallback in supported else supported[0]


def resolve_document(doc_id: str, lang: str) -> tuple[LegalDocument | None, str]:
    """The current ``doc_id`` for ``lang``, falling back deliberately.

    Returns ``(document, resolved_lang)``. Fallback order is the requested
    language, then German (binding), then English — a visitor reading Romanian
    must still be shown *something* rather than a 404, and the something they
    are shown is the text that actually governs.

    Used only by the public read endpoints. The consent path calls
    :func:`require_document`, which does not fall back at all.
    """
    wanted = normalise_lang(lang)
    for candidate in (wanted, *LANG_FALLBACK_ORDER):
        document = LegalDocument.objects.current_for(doc_id, candidate)
        if document is not None:
            return document, candidate
    document = (
        LegalDocument.objects.filter(doc_id=doc_id, is_current=True)
        .order_by("lang")
        .first()
    )
    return (document, document.lang) if document is not None else (None, wanted)


def require_document(doc_id: str, lang: str = BINDING_LANGUAGE) -> LegalDocument:
    """The current document, or :class:`LegalDocumentUnavailable`.

    No fallback: a consent record must name the exact text it is evidence for.
    Quietly substituting the English translation when the German original is
    missing would put an unenforceable hash in the ledger.
    """
    document = LegalDocument.objects.current_for(doc_id, lang)
    if document is None:
        logger.error(
            "Legal document %s/%s is not seeded; run `manage.py seed_legal`.",
            doc_id,
            lang,
        )
        raise LegalDocumentUnavailable()
    return document


def current_terms_version() -> str:
    """The version string the client should be displaying, or ``""``.

    Handed to anonymous callers (it is printed in every copy of the document
    anyway), so it must never raise — an unseeded database returns an empty
    string and the staleness check then simply does not fire.
    """
    document = LegalDocument.objects.current_for(DocId.TERMS.value, BINDING_LANGUAGE)
    return document.version if document is not None else ""


# ---------------------------------------------------------------------------
# Writing consent
# ---------------------------------------------------------------------------


def _clean_user_agent(value: str | None) -> str:
    from apps.legal.models import MAX_USER_AGENT_LENGTH

    return (value or "").replace("\x00", "").strip()[:MAX_USER_AGENT_LENGTH]


def _write(
    *,
    user,
    consent_type: str,
    granted: bool,
    document: LegalDocument | None,
    ip: str | None,
    user_agent: str | None,
    ui_language: str | None,
) -> ConsentRecord:
    return ConsentRecord.objects.create(
        user=user,
        type=consent_type,
        granted=granted,
        doc_version=document.version if document is not None else "",
        content_hash=document.content_hash if document is not None else "",
        doc_lang=document.lang if document is not None else "",
        ip=ip or None,
        user_agent=_clean_user_agent(user_agent),
        ui_language=normalise_lang(ui_language) if ui_language else "",
    )


def _already_granted(user, consent_type: str, version: str) -> bool:
    """True when the newest record for this type already grants that version.

    Makes :func:`record_registration_consent` idempotent, which matters because
    the OTP verify path can legitimately re-run (a retried request, a
    re-entrant transaction) and the ledger should read as one decision, not
    three.
    """
    latest = (
        ConsentRecord.objects.for_user(user)
        .of_type(consent_type)
        .order_by("-created_at", "-id")
        .first()
    )
    return latest is not None and latest.granted and latest.doc_version == version


@transaction.atomic
def record_registration_consent(
    user,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    ui_language: str | None = None,
    client_terms_version: str | None = None,
) -> list[ConsentRecord]:
    """Write the ``terms`` + ``privacy`` consents taken at first login.

    Call this from inside the same ``transaction.atomic`` block in
    ``apps.accounts`` that creates the user on a first successful OTP verify.

    ``client_terms_version`` is whatever version string the browser said it had
    rendered. It is used for **one** thing — detecting that the user accepted a
    superseded text, which raises :class:`TermsOutdated` (409
    ``terms_outdated``) — and is never stored. ``None`` or ``""`` skips the
    check, so a client that does not send it still works.

    Raises :class:`LegalDocumentUnavailable` when the binding German documents
    have not been seeded. That is intentional and fails closed: better a loud
    503 than a consent record nobody could rely on.
    """
    terms = require_document(DocId.TERMS.value, BINDING_LANGUAGE)
    privacy = require_document(DocId.PRIVACY.value, BINDING_LANGUAGE)

    wanted = (client_terms_version or "").strip()
    if wanted and wanted != terms.version:
        logger.info(
            "Rejected registration consent: client had terms %r, current is %r",
            wanted,
            terms.version,
        )
        raise TermsOutdated()

    records: list[ConsentRecord] = []
    for consent_type, document in (
        (ConsentType.TERMS.value, terms),
        (ConsentType.PRIVACY.value, privacy),
    ):
        if _already_granted(user, consent_type, document.version):
            continue
        records.append(
            _write(
                user=user,
                consent_type=consent_type,
                granted=True,
                document=document,
                ip=ip,
                user_agent=user_agent,
                ui_language=ui_language,
            )
        )
    return records


def grant_consent(
    user,
    consent_type: str,
    *,
    granted: bool = True,
    ip: str | None = None,
    user_agent: str | None = None,
    ui_language: str | None = None,
) -> ConsentRecord:
    """Append one consent decision for a revocable consent type.

    The document the decision is evidenced against is resolved server-side from
    :data:`apps.legal.models.CONSENT_DOCUMENT`, in the binding language. If that
    document has not been seeded the record is still written, with an empty
    hash: unlike registration consent these opt-ins are not the contract, and
    blocking a user from switching analytics off because a seed run is missing
    would be perverse.
    """
    if consent_type in REGISTRATION_CONSENT_TYPES:
        raise ConsentNotRevocable(
            detail=(
                "Acceptance of the terms and privacy policy is recorded at "
                "registration and cannot be changed here."
            )
        )
    if consent_type not in REVOCABLE_CONSENT_TYPES:
        raise ApiError(
            ErrorCode.INVALID, "Unknown consent type.", status.HTTP_400_BAD_REQUEST
        )

    doc_id = CONSENT_DOCUMENT.get(consent_type)
    document = (
        LegalDocument.objects.current_for(doc_id, BINDING_LANGUAGE) if doc_id else None
    )
    return _write(
        user=user,
        consent_type=consent_type,
        granted=bool(granted),
        document=document,
        ip=ip,
        user_agent=user_agent,
        ui_language=ui_language,
    )


def revoke_consent(user, consent_type: str, **kwargs) -> ConsentRecord:
    """Append a ``granted=False`` decision. Nothing is deleted."""
    return grant_consent(user, consent_type, granted=False, **kwargs)


# ---------------------------------------------------------------------------
# Reading consent
# ---------------------------------------------------------------------------


def current_consent(user) -> dict[str, ConsentRecord]:
    """The newest decision per consent type for ``user``."""
    return ConsentRecord.objects.latest_per_type(user)


def has_consent(user, consent_type: str) -> bool:
    """Whether ``user`` currently grants ``consent_type``.

    The send pipeline hard-blocks on ``has_consent(user, "send_on_behalf")``
    (spec §10). Absence of a record is absence of consent — never a default.
    """
    latest = (
        ConsentRecord.objects.for_user(user)
        .of_type(consent_type)
        .order_by("-created_at", "-id")
        .first()
    )
    return bool(latest and latest.granted)


def consent_history(user, *, types: Iterable[str] | None = None, limit: int = 100):
    """Recent ledger entries, newest first."""
    queryset = ConsentRecord.objects.for_user(user)
    if types:
        queryset = queryset.filter(type__in=list(types))
    return queryset.order_by("-created_at", "-id")[:limit]
