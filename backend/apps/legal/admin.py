"""Django admin for legal documents and the consent ledger.

Both models are read-mostly by design. ``LegalDocument`` is editable (so a
typo can be fixed in an emergency) but shows a computed *hash matches* column,
because editing the text without re-running ``seed_legal`` silently detaches
every consent record that points at the old digest.

``ConsentRecord`` is add/change/delete-disabled: the model itself refuses
updates, and offering a form that always errors is worse than offering none.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.legal.models import ConsentRecord, LegalDocument


@admin.register(LegalDocument)
class LegalDocumentAdmin(admin.ModelAdmin):
    list_display = (
        "doc_id",
        "lang",
        "version",
        "is_current",
        "effective_from",
        "short_hash",
        "hash_ok",
    )
    list_filter = ("doc_id", "lang", "is_current")
    search_fields = ("doc_id", "title", "version", "content_hash")
    readonly_fields = ("content_hash", "created_at", "updated_at", "hash_ok")
    ordering = ("doc_id", "lang", "-effective_from")

    @admin.display(description=_("hash"))
    def short_hash(self, obj: LegalDocument) -> str:
        return (obj.content_hash or "")[:12]

    @admin.display(description=_("hash matches text"), boolean=True)
    def hash_ok(self, obj: LegalDocument) -> bool:
        return obj.hash_matches_content


@admin.register(ConsentRecord)
class ConsentRecordAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "user",
        "type",
        "granted",
        "doc_version",
        "doc_lang",
        "ui_language",
        "ip",
    )
    list_filter = ("type", "granted", "doc_lang")
    search_fields = ("type", "doc_version", "content_hash")
    date_hierarchy = "created_at"
    raw_id_fields = ("user",)
    readonly_fields = tuple(
        field.name for field in ConsentRecord._meta.fields
    )

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("user")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
