"""Django admin for legal documents and the consent ledger.

Both models are **strictly read-only here**, for two different reasons.

``LegalDocument`` is seeded from ``web/legal.js`` by ``manage.py seed_legal``
and is tamper-evident: every :class:`~apps.legal.models.ConsentRecord` stores a
copy of the ``content_hash`` of the text it was granted against. Editing a
document's text in this admin does not update that hash, so the row would keep
claiming a digest its own content no longer produces — the ``hash matches text``
column would flip to ✗ and every consent record pointing at the old digest would
be orphaned evidence. The supported way to change a document is to change
``web/legal.js`` and re-run ``seed_legal``, which mints a **new version** and
leaves the old one intact for the consents that reference it. Hence: no add, no
change, no delete, and an ``admin_note`` that says so where somebody looking for
the Save button will read it.

``ConsentRecord`` is append-only by model contract —
:meth:`ConsentRecord.save` raises on an UPDATE. A form that always errors is
worse than no form, so admin does not offer one. This table is the GDPR
*Nachweis* (Art. 7(1)): its value is precisely that nobody, including staff, can
revise it. Correcting a consent means appending a new record.

Note the asymmetry with :mod:`apps.accounts.admin`: ``content_hash`` is shown
here **in full**, because it is public evidence over public text and an operator
has to be able to eyeball a consent record's hash against the document's. The
OTP ``code_hash`` next door is the opposite — a credential — and is never
rendered at all.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.html import format_html, format_html_join
from django.utils.translation import gettext_lazy as _

from apps.common.admin_support import EMPTY, ReadOnlyAdminMixin, scrollable_block
from apps.legal.models import ConsentRecord, LegalDocument

#: Characters of ``content_hash`` shown in list columns. Twelve is unambiguous
#: in practice and short enough to scan a column of them.
HASH_PREFIX_LENGTH = 12


@admin.register(LegalDocument)
class LegalDocumentAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """One version of one document in one language. Seeded, never edited."""

    admin_note = _(
        "Read-only on purpose. These rows are seeded by `manage.py seed_legal` "
        "from web/legal.js and are tamper-evident: every consent record stores "
        "the content hash of the text it was granted against, so editing a "
        "document here would orphan that evidence. To change a text, change "
        "web/legal.js and re-run seed_legal — it mints a new version and leaves "
        "the old one intact."
    )

    list_display = (
        "doc_id",
        "lang",
        "version",
        "is_current",
        "effective_from",
        "short_hash",
        "section_count",
        "hash_ok",
    )
    list_display_links = ("doc_id", "version")
    list_filter = ("doc_id", "lang", "is_current", "effective_from")
    search_fields = ("doc_id", "title", "version", "content_hash")
    date_hierarchy = "effective_from"
    ordering = ("doc_id", "lang", "-effective_from", "-id")
    list_per_page = 50
    # No FK on this model — a LegalDocument deliberately references nothing, so
    # that it can be superseded without dragging anything with it.
    readonly_fields = (
        "doc_id",
        "lang",
        "version",
        "title",
        "note",
        "sections_preview",
        "content_hash",
        "hash_ok",
        "effective_from",
        "is_current",
        "created_at",
        "updated_at",
    )
    fieldsets = (
        (
            _("Identity"),
            {
                "fields": ("doc_id", "lang", "version", "title"),
                "description": _(
                    "One row per (document, language, version). At most one may "
                    "be current per (document, language) — enforced by a partial "
                    "unique index, not by application code."
                ),
            },
        ),
        (_("Content"), {"fields": ("note", "sections_preview")}),
        (
            _("Integrity"),
            {
                "fields": ("content_hash", "hash_ok"),
                "description": _(
                    "The hash is computed over doc_id, lang, version, title and "
                    "sections. A ✗ below means the stored text no longer produces "
                    "the stored digest — re-run seed_legal."
                ),
            },
        ),
        (
            _("Lifecycle"),
            {"fields": ("effective_from", "is_current", "created_at", "updated_at")},
        ),
    )

    @admin.display(description=_("hash"), ordering="content_hash")
    def short_hash(self, obj: LegalDocument) -> str:
        return (obj.content_hash or "")[:HASH_PREFIX_LENGTH] or EMPTY

    @admin.display(description=_("hash matches text"), boolean=True)
    def hash_ok(self, obj: LegalDocument) -> bool | None:
        """✓ / ✗ / unknown.

        ``hash_matches_content`` re-hashes the row, and ``compute_content_hash``
        raises on a ``sections`` value it cannot canonicalise — a bare string, a
        one-element pair, a number. ``sections`` is a ``JSONField`` with no
        schema enforcement, so such a row is reachable from a data migration, a
        shell session or a seeding bug, and this admin is precisely the page
        somebody would open to *diagnose* it. Letting the exception through
        would turn both the changelist and the change form into a 500 for every
        row on the page.

        ``None`` renders as the neutral "unknown" icon and says what is true:
        the digest could not be recomputed, so this is neither a match nor a
        mismatch.
        """
        try:
            return obj.hash_matches_content
        except (TypeError, ValueError):
            return None

    @admin.display(description=_("sections"))
    def section_count(self, obj: LegalDocument) -> int:
        sections = obj.sections
        return len(sections) if isinstance(sections, list) else 0

    @admin.display(description=_("sections"))
    def sections_preview(self, obj: LegalDocument):
        """Render ``sections`` as readable prose instead of a JSON blob.

        ``sections`` is a list of ``{"heading": …, "body": …}`` dicts, but it is
        a ``JSONField`` with no schema enforcement, so anything at all can be in
        there — non-dict entries and missing keys are normalised rather than
        allowed to raise inside a change form. Everything goes through
        ``format_html``, so markup in a document body is displayed, never
        executed.
        """
        sections = obj.sections
        if not isinstance(sections, list) or not sections:
            return EMPTY

        normalised = []
        for section in sections:
            if isinstance(section, dict):
                heading = str(section.get("heading") or "").strip()
                body = str(section.get("body") or "").strip()
            else:
                heading, body = "", str(section).strip()
            normalised.append((heading or _("(untitled section)"), body))

        items = format_html_join(
            "",
            '<li style="margin:0 0 1rem"><strong>{}</strong>'
            '<div style="white-space:pre-wrap;margin-top:.25rem">{}</div></li>',
            normalised,
        )
        return scrollable_block(
            format_html('<ol style="margin:0;padding-left:1.25rem">{}</ol>', items)
        )


@admin.register(ConsentRecord)
class ConsentRecordAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """The GDPR consent ledger. Append-only, therefore view-only."""

    admin_note = _(
        "Append-only evidence (Art. 7(1) GDPR): the model refuses an UPDATE, so "
        "this admin offers no form at all. A consent decision is corrected by "
        "appending a new record — granted=False to revoke — never by editing "
        "history. Records are erased only by deleting the user (Art. 17)."
    )

    list_display = (
        "created_at",
        "user",
        "type",
        "granted",
        "doc_version",
        "doc_lang",
        "short_hash",
        "ui_language",
        "ip",
    )
    list_display_links = ("created_at",)
    list_filter = ("type", "granted", "doc_lang", "ui_language", "created_at")
    search_fields = ("user__email", "type", "doc_version", "content_hash")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    list_per_page = 50
    # `user` is the only FK and it is rendered in every row. `list_select_related`
    # replaces the hand-written get_queryset override this file used to carry.
    # There is no raw_id/autocomplete widget to configure: the change form is
    # permanently read-only, so no FK widget is ever rendered.
    list_select_related = ("user",)
    readonly_fields = tuple(field.name for field in ConsentRecord._meta.fields)
    fieldsets = (
        (_("Who"), {"fields": ("user", "created_at")}),
        (
            _("Decision"),
            {
                "fields": ("type", "granted"),
                "description": _(
                    "granted=False is a revocation. terms and privacy are "
                    "collected at registration and are not revocable through the "
                    "API — withdrawing them is account deletion."
                ),
            },
        ),
        (
            _("Evidence"),
            {
                "fields": ("doc_version", "doc_lang", "content_hash"),
                "description": _(
                    "Copied in at write time, not referenced by foreign key: the "
                    "record has to stay meaningful after the document row is "
                    "superseded or re-seeded. Match content_hash against Legal "
                    "documents to see the exact text."
                ),
            },
        ),
        (_("Request metadata"), {"fields": ("ip", "user_agent", "ui_language")}),
    )

    @admin.display(description=_("hash"), ordering="content_hash")
    def short_hash(self, obj: ConsentRecord) -> str:
        return (obj.content_hash or "")[:HASH_PREFIX_LENGTH] or EMPTY
