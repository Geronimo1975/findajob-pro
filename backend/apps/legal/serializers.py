"""Serializers for the legal + consent API.

The public document shape deliberately mirrors ``web/legal.js`` so the frontend
can render a seeded document and a bundled fallback with the same code path::

    { doc_id, lang, version, title, updated, note, sections: [{heading, body}],
      content_hash, effective_from, is_binding, requested_lang }

``content_hash`` is exposed on purpose: it is not a secret, and publishing it is
what lets a user (or a court) check that the text they were shown is the text
their consent record points at.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.legal.models import (
    BINDING_LANGUAGE,
    REVOCABLE_CONSENT_TYPES,
    ConsentRecord,
    ConsentType,
    LegalDocument,
)


class SectionSerializer(serializers.Serializer):
    """One ``[heading, body]`` pair, however the row happens to store it."""

    heading = serializers.CharField()
    body = serializers.CharField()

    def to_representation(self, instance) -> dict:
        if isinstance(instance, dict):
            return {
                "heading": str(instance.get("heading", instance.get("title", "")) or ""),
                "body": str(instance.get("body", instance.get("text", "")) or ""),
            }
        if isinstance(instance, (list, tuple)) and len(instance) >= 2:
            return {"heading": str(instance[0] or ""), "body": str(instance[1] or "")}
        return {"heading": "", "body": str(instance or "")}


class LegalDocumentSummarySerializer(serializers.ModelSerializer):
    """Index entry — everything except the body text."""

    is_binding = serializers.SerializerMethodField()

    class Meta:
        model = LegalDocument
        fields = (
            "doc_id",
            "lang",
            "version",
            "title",
            "content_hash",
            "effective_from",
            "is_binding",
        )
        read_only_fields = fields

    def get_is_binding(self, obj: LegalDocument) -> bool:
        return obj.lang == BINDING_LANGUAGE


class LegalDocumentSerializer(LegalDocumentSummarySerializer):
    """Full document, including the ordered sections."""

    sections = serializers.SerializerMethodField()
    requested_lang = serializers.SerializerMethodField()

    class Meta(LegalDocumentSummarySerializer.Meta):
        fields = LegalDocumentSummarySerializer.Meta.fields + (
            "note",
            "sections",
            "requested_lang",
        )
        read_only_fields = fields

    def get_sections(self, obj: LegalDocument) -> list[dict]:
        return SectionSerializer(obj.sections or [], many=True).data

    def get_requested_lang(self, obj: LegalDocument) -> str:
        """What the caller asked for — may differ from ``lang`` on a fallback."""
        return self.context.get("requested_lang", obj.lang)


class ConsentRecordSerializer(serializers.ModelSerializer):
    """One ledger entry.

    ``ip`` and ``user_agent`` are **not** exposed. They are in the record for
    evidentiary value, and echoing them back to the browser adds nothing the
    browser does not already know while widening what an XSS could read.
    """

    class Meta:
        model = ConsentRecord
        fields = (
            "id",
            "type",
            "granted",
            "doc_version",
            "doc_lang",
            "content_hash",
            "ui_language",
            "created_at",
        )
        read_only_fields = fields


class ConsentWriteSerializer(serializers.Serializer):
    """Body of ``POST /api/v1/consent/``.

    Only the revocable types are accepted: ``terms`` and ``privacy`` are taken
    at registration and are not user-settable here (see
    :class:`apps.legal.services.ConsentNotRevocable`), so they are not even in
    the choice list — an attempt to POST them is a plain validation error rather
    than something the view has to special-case.
    """

    type = serializers.ChoiceField(choices=REVOCABLE_CONSENT_TYPES)
    granted = serializers.BooleanField()
    lang = serializers.CharField(required=False, allow_blank=True, max_length=8)

    def validate_type(self, value: str) -> str:
        # ChoiceField already restricts the set; this keeps the error message
        # aligned with the documented vocabulary if the tuple ever changes.
        if value not in ConsentType.values:
            raise serializers.ValidationError("Unknown consent type.")
        return value
