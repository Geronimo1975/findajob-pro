"""Jobs serializers — emit exactly the shapes ``web/api.js`` consumes.

The contract rule (architecture spec §3) is that the frontend goes live by
swapping fixture bodies for fetches, so the wire shape here matches the
fixtures byte-for-byte where they were right, and fixes only the audited
divergences that live server-side:

- The model stores ``platform_specific_data`` / ``rate_limit_config``; the
  wire keeps the frontend's ``psd`` / ``rate_limit`` (a display string —
  the structured config is an ingest concern the UI never edits).
- ``status`` / ``sent_manually`` / ``sent_at`` are application state, kept on
  :class:`~apps.jobs.models.Application`; the job row flattens the calling
  member's application over the shared listing, defaulting to ``pending``.
- Timestamps ride as epoch **milliseconds**, which is what the shell's
  ``ago()`` arithmetic and the fixtures already use.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.jobs.models import Application, Document, JobListing, Platform


def _epoch_ms(dt) -> int | None:
    return int(dt.timestamp() * 1000) if dt else None


def rate_limit_display(config: dict) -> str:
    """``{"daily": 60, "delay_seconds": 5}`` → ``"60/day, 5s delay"``."""
    if not config:
        return "—"
    if config.get("feed_only"):
        return "feed only"
    parts = []
    if config.get("daily"):
        parts.append(f"{config['daily']}/day")
    if config.get("delay_seconds"):
        parts.append(f"{config['delay_seconds']}s delay")
    return ", ".join(parts) or "—"


class PlatformSerializer(serializers.ModelSerializer):
    id = serializers.CharField(source="slug")
    rate_limit = serializers.SerializerMethodField()

    class Meta:
        model = Platform
        fields = (
            "id", "name", "connector_type", "auth_method", "apply_mechanism",
            "risk", "auto_send", "is_active", "rate_limit", "note",
        )

    def get_rate_limit(self, obj: Platform) -> str:
        return rate_limit_display(obj.rate_limit_config or {})


class JobRowSerializer(serializers.ModelSerializer):
    """One dashboard row: the shared listing + this member's application.

    Context must carry ``applications``: a ``{job_listing_id: Application}``
    map already scoped to the requesting member — this serializer never
    queries, so a caller cannot accidentally leak another tenant's state.
    """

    platform = serializers.SlugRelatedField(slug_field="slug", read_only=True)
    psd = serializers.JSONField(source="platform_specific_data")
    scraped_at = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    sent_manually = serializers.SerializerMethodField()
    sent_at = serializers.SerializerMethodField()

    class Meta:
        model = JobListing
        fields = (
            "id", "platform", "title", "company", "url", "location", "salary",
            "description", "requirements", "tags", "psd", "scraped_at",
            "status", "sent_manually", "sent_at",
        )

    def _application(self, obj: JobListing) -> Application | None:
        return self.context.get("applications", {}).get(obj.pk)

    def get_scraped_at(self, obj: JobListing) -> int | None:
        return _epoch_ms(obj.scraped_at)

    def get_status(self, obj: JobListing) -> str:
        app = self._application(obj)
        return app.status if app else "pending"

    def get_sent_manually(self, obj: JobListing) -> bool:
        app = self._application(obj)
        return bool(app and app.sent_manually)

    def get_sent_at(self, obj: JobListing) -> int | None:
        app = self._application(obj)
        return _epoch_ms(app.sent_at) if app else None


def _format_size(num_bytes: int) -> str:
    """``184320`` -> ``"184 KB"``, ``2202009`` -> ``"2.1 MB"`` — the fixture's
    display style (``web/api.js``'s ``DOCUMENTS``), not raw byte counts."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    return f"{max(1, round(num_bytes / 1024))} KB"


class DocumentSerializer(serializers.ModelSerializer):
    """One document row. Same shape for the list and the upload response."""

    size = serializers.SerializerMethodField()
    updated = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = ("id", "name", "kind", "tags", "size", "updated", "ats_score")

    def get_size(self, obj: Document) -> str:
        return _format_size(obj.size_bytes)

    def get_updated(self, obj: Document) -> str:
        return obj.updated_at.strftime("%b %d, %Y")
