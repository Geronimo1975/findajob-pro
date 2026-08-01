"""Versioned legal documents and the append-only consent ledger.

Implementation spec §10::

    apps/legal ships LegalDocument (doc_id, lang, version, content_hash,
    effective_from, is_current) seeded from web/legal.js, and ConsentRecord
    (append-only: user, type, granted, doc_version, content_hash, ip,
    user_agent, ui_language, created_at).

Two invariants carry the whole module:

1. **Exactly one current version per (doc_id, lang).** Enforced in the database
   by a *partial* unique index, not by application code — a half-finished seed
   run or a hand-edit in the admin must not be able to leave two "current"
   terms in place, because then the server could not say which text a new user
   agreed to.
2. **A consent record is never updated.** :meth:`ConsentRecord.save` refuses to
   write an existing row. Consent is evidence; correcting it means appending a
   new row (a revocation, or a fresh grant), never rewriting history.

Django 6.0 notes: ``CheckConstraint`` takes ``condition=`` (``check=`` was
removed), and ``Model.save()`` no longer accepts positional arguments.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.legal.hashing import compute_content_hash

MAX_DOC_ID_LENGTH = 32
MAX_LANG_LENGTH = 8
MAX_VERSION_LENGTH = 32
MAX_TITLE_LENGTH = 200
MAX_CONTENT_HASH_LENGTH = 64
MAX_USER_AGENT_LENGTH = 400

#: The language whose text is legally binding (spec §10). Never resolve a
#: registration consent against anything else, whatever UI language the visitor
#: happens to be reading.
BINDING_LANGUAGE = "de"


class DocId(models.TextChoices):
    """The six documents in ``web/legal.js`` — ``LEGAL_DOCS``, in order."""

    TERMS = "terms", _("Terms & Conditions (AGB)")
    PRIVACY = "privacy", _("Privacy Policy")
    COOKIES = "cookies", _("Cookie Policy")
    IMPRESSUM = "impressum", _("Impressum")
    WITHDRAWAL = "withdrawal", _("Withdrawal Policy")
    DPA = "dpa", _("Data Processing Agreement")


#: Iteration order for the index endpoint and the seed command.
DOC_ID_ORDER: tuple[str, ...] = tuple(choice.value for choice in DocId)


class Lang(models.TextChoices):
    """Mirrors ``settings.LANGUAGES``: EN default, DE binding, RO courtesy."""

    EN = "en", _("English")
    DE = "de", _("Deutsch")
    RO = "ro", _("Română")


LANG_ORDER: tuple[str, ...] = tuple(choice.value for choice in Lang)

#: Preference order when a requested language has no document: the binding text
#: first, then English. Never silently return nothing.
LANG_FALLBACK_ORDER: tuple[str, ...] = (BINDING_LANGUAGE, "en")


class ConsentType(models.TextChoices):
    """What a consent record is *about*.

    ``TERMS`` and ``PRIVACY`` are collected once, at registration, inside the
    same transaction that creates the user. The rest are opt-ins the user grants
    and revokes later; ``SEND_ON_BEHALF`` is the one the send pipeline
    hard-blocks on (spec §10).
    """

    TERMS = "terms", _("Terms & Conditions")
    PRIVACY = "privacy", _("Privacy Policy")
    SEND_ON_BEHALF = "send_on_behalf", _("Send applications on my behalf")
    MARKETING = "marketing", _("Product & marketing email")
    ANALYTICS = "analytics", _("Analytics cookies")
    FUNCTIONAL = "functional", _("Functional cookies")


#: Collected at registration, and *not* revocable through the consent API:
#: withdrawing acceptance of the terms is account deletion, which is a
#: different, deliberate flow with its own erasure duties (Art. 17 GDPR).
REGISTRATION_CONSENT_TYPES: tuple[str, ...] = (
    ConsentType.TERMS.value,
    ConsentType.PRIVACY.value,
)

#: Freely grantable and revocable through ``/api/v1/consent/``.
REVOCABLE_CONSENT_TYPES: tuple[str, ...] = (
    ConsentType.SEND_ON_BEHALF.value,
    ConsentType.MARKETING.value,
    ConsentType.ANALYTICS.value,
    ConsentType.FUNCTIONAL.value,
)

#: Which document a consent type is evidenced against. The hash of that
#: document is what gets stored on the record, so "what did they agree to" has
#: an answer years later even after the text has been rewritten twice.
#:
#: ``send_on_behalf`` and ``marketing`` point at the privacy policy because that
#: is where their legal basis (Art. 6 (1) a GDPR) is described; the two cookie
#: consents point at the cookie policy (§ 25 TTDSG).
CONSENT_DOCUMENT: dict[str, str] = {
    ConsentType.TERMS.value: DocId.TERMS.value,
    ConsentType.PRIVACY.value: DocId.PRIVACY.value,
    ConsentType.SEND_ON_BEHALF.value: DocId.PRIVACY.value,
    ConsentType.MARKETING.value: DocId.PRIVACY.value,
    ConsentType.ANALYTICS.value: DocId.COOKIES.value,
    ConsentType.FUNCTIONAL.value: DocId.COOKIES.value,
}


# ---------------------------------------------------------------------------
# LegalDocument
# ---------------------------------------------------------------------------


class LegalDocumentQuerySet(models.QuerySet):
    def current(self) -> "LegalDocumentQuerySet":
        return self.filter(is_current=True)

    def for_doc(self, doc_id: str) -> "LegalDocumentQuerySet":
        return self.filter(doc_id=doc_id)


class LegalDocumentManager(models.Manager.from_queryset(LegalDocumentQuerySet)):
    """Adds the one lookup every caller actually wants."""

    def current_for(self, doc_id: str, lang: str) -> "LegalDocument | None":
        """The live version of ``doc_id`` in ``lang``, or ``None``.

        No fallback here on purpose — :func:`apps.legal.services.resolve_document`
        owns the fallback policy, and the consent path must never fall back at
        all (it needs the binding German text or nothing).
        """
        return self.filter(doc_id=doc_id, lang=lang, is_current=True).first()


class LegalDocument(models.Model):
    """One version of one document in one language.

    Rows are effectively immutable once a :class:`ConsentRecord` references
    their ``content_hash``. ``manage.py seed_legal`` refuses to overwrite a
    stored version whose text has drifted — the fix is to bump the document's
    date in ``web/legal.js``, which mints a new version.
    """

    doc_id = models.CharField(
        _("document"), max_length=MAX_DOC_ID_LENGTH, choices=DocId.choices
    )
    lang = models.CharField(
        _("language"), max_length=MAX_LANG_LENGTH, choices=Lang.choices
    )
    version = models.CharField(
        _("version"),
        max_length=MAX_VERSION_LENGTH,
        help_text=_("Derived from the document date, e.g. 2026-08-01."),
    )
    title = models.CharField(_("title"), max_length=MAX_TITLE_LENGTH)
    sections = models.JSONField(
        _("sections"),
        default=list,
        help_text=_('Ordered list of {"heading": …, "body": …} objects.'),
    )
    note = models.TextField(
        _("note"),
        blank=True,
        help_text=_("Per-language notice, e.g. that DE is the binding version."),
    )
    content_hash = models.CharField(
        _("content hash"), max_length=MAX_CONTENT_HASH_LENGTH, db_index=True
    )
    effective_from = models.DateTimeField(_("effective from"))
    is_current = models.BooleanField(_("is current"), default=False)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = LegalDocumentManager()

    class Meta:
        verbose_name = _("legal document")
        verbose_name_plural = _("legal documents")
        ordering = ("doc_id", "lang", "-effective_from", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("doc_id", "lang", "version"),
                name="uniq_legaldocument_version",
            ),
            # Partial unique index: at most ONE live version per document and
            # language. Postgres and SQLite both support this; it is the only
            # thing standing between a botched seed run and an ambiguous answer
            # to "which text did this user accept?".
            models.UniqueConstraint(
                fields=("doc_id", "lang"),
                condition=models.Q(is_current=True),
                name="uniq_current_legaldocument",
            ),
            # Django 6.0: condition=, not check= (removed).
            models.CheckConstraint(
                condition=~models.Q(content_hash=""),
                name="legaldocument_content_hash_not_blank",
            ),
            models.CheckConstraint(
                condition=~models.Q(version=""),
                name="legaldocument_version_not_blank",
            ),
        ]
        indexes = [
            models.Index(
                fields=["doc_id", "lang", "-effective_from"],
                name="legaldoc_doc_lang_eff_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.doc_id}/{self.lang}@{self.version}"

    # -- helpers ----------------------------------------------------------

    def compute_hash(self) -> str:
        """The digest this row's content *should* have."""
        return compute_content_hash(
            doc_id=self.doc_id,
            lang=self.lang,
            version=self.version,
            title=self.title,
            sections=self.sections or [],
        )

    @property
    def hash_matches_content(self) -> bool:
        """False means the stored text was edited without re-hashing."""
        return bool(self.content_hash) and self.content_hash == self.compute_hash()


# ---------------------------------------------------------------------------
# ConsentRecord
# ---------------------------------------------------------------------------


class ConsentRecordQuerySet(models.QuerySet):
    def for_user(self, user) -> "ConsentRecordQuerySet":
        return self.filter(user=user)

    def of_type(self, consent_type: str) -> "ConsentRecordQuerySet":
        return self.filter(type=consent_type)

    def latest_per_type(self, user) -> dict[str, "ConsentRecord"]:
        """The newest record for each consent type this user has touched.

        Ordering is explicit (``-created_at, -id``) rather than inherited: the
        ledger can hold several rows written inside one transaction, whose
        ``created_at`` values are identical to the microsecond, and the id is
        the only remaining tiebreak.
        """
        newest: dict[str, ConsentRecord] = {}
        for record in self.for_user(user).order_by("created_at", "id"):
            newest[record.type] = record
        return newest


class ConsentRecord(models.Model):
    """One immutable entry in the consent ledger.

    Append-only in the strict sense: :meth:`save` refuses to write a row that
    already exists. Deletion is *not* blocked — Art. 17 GDPR erasure and the
    ``on_delete=CASCADE`` that implements it must keep working.

    ``doc_version`` and ``content_hash`` are copied in at write time rather than
    referenced by foreign key: the record has to survive the document row being
    superseded, re-seeded, or removed, and a snapshot is what makes it evidence
    rather than a pointer.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="consent_records",
        verbose_name=_("user"),
    )
    type = models.CharField(
        _("type"), max_length=MAX_DOC_ID_LENGTH, choices=ConsentType.choices
    )
    granted = models.BooleanField(_("granted"))
    doc_version = models.CharField(
        _("document version"), max_length=MAX_VERSION_LENGTH, blank=True
    )
    content_hash = models.CharField(
        _("content hash"), max_length=MAX_CONTENT_HASH_LENGTH, blank=True
    )
    #: The document language the consent was evidenced against — always the
    #: binding one for registration consents, whatever the UI was showing.
    doc_lang = models.CharField(
        _("document language"), max_length=MAX_LANG_LENGTH, blank=True
    )
    ip = models.GenericIPAddressField(_("ip address"), null=True, blank=True)
    user_agent = models.CharField(
        _("user agent"), max_length=MAX_USER_AGENT_LENGTH, blank=True
    )
    ui_language = models.CharField(
        _("interface language"), max_length=MAX_LANG_LENGTH, blank=True
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    objects = ConsentRecordQuerySet.as_manager()

    class Meta:
        verbose_name = _("consent record")
        verbose_name_plural = _("consent records")
        ordering = ("-created_at", "-id")
        indexes = [
            models.Index(
                fields=["user", "type", "-created_at"], name="consent_user_type_idx"
            ),
            models.Index(fields=["type", "-created_at"], name="consent_type_idx"),
        ]

    def __str__(self) -> str:
        verb = "granted" if self.granted else "revoked"
        return f"{self.user_id} {verb} {self.type} @{self.doc_version or '-'}"

    # Django 6.0 removed positional arguments to Model.save().
    def save(self, **kwargs) -> None:
        if self.pk is not None and not self._state.adding:
            raise ValueError(
                "ConsentRecord is append-only: a consent decision may not be "
                "edited. Write a new record (granted=False to revoke) instead."
            )
        # Belt and braces — an explicit `force_update`, or a caller that resets
        # `_state.adding` by hand, must not be able to smuggle an UPDATE past
        # the check above.
        if kwargs.get("force_update"):
            raise ValueError("ConsentRecord is append-only: force_update is refused.")
        kwargs["force_insert"] = True
        super().save(**kwargs)
