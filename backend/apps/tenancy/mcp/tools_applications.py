"""Application tools (spec §5). Every read starts from
``Application.objects.for_org(ctx.org).filter(user=ctx.user)``."""
from __future__ import annotations

from rest_framework import serializers

from django.db.models import Prefetch

from apps.jobs.models import Application, ApplicationDocument, ApplicationStatus, Document, JobListing
from apps.tenancy.mcp.serializers import application_out
from apps.tenancy.mcp.tools import ToolContext, ToolError, register, tool_ok, validate
from apps.tenancy.models import AuditLog

_STATUSES = [c for c, _ in ApplicationStatus.choices]


def own_applications(ctx: ToolContext):
    # Prefetch the through-model rows for the documents overlay so
    # ``application_out`` on a page of N applications costs one extra query,
    # not N (ApplicationDocument is a plain manager; no org-scope to lose).
    docs_prefetch = Prefetch(
        "applicationdocument_set",
        queryset=ApplicationDocument.objects.select_related("document").order_by("position"),
    )
    return (Application.objects.for_org(ctx.org).filter(user=ctx.user)
            .select_related("job_listing", "job_listing__platform")
            .prefetch_related(docs_prefetch))


def own_application(ctx: ToolContext, application_id: int) -> Application:
    app = own_applications(ctx).filter(pk=application_id).first()
    if app is None:
        raise ToolError("application_not_found", "No such application in this workspace.")
    return app


class ListApplicationsArgs(serializers.Serializer):
    status = serializers.ChoiceField(choices=_STATUSES, required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=50, default=20)


class ApplicationIdArgs(serializers.Serializer):
    application_id = serializers.IntegerField(min_value=1)


@register("list_applications", scope="applications:read",
          description="This member's applications in the workspace. Filter: status.",
          input_schema={"type": "object", "properties": {
              "status": {"type": "string", "enum": _STATUSES},
              "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}},
              "additionalProperties": False})
def list_applications(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(ListApplicationsArgs, arguments)
    qs = own_applications(ctx)
    if args.get("status"):
        qs = qs.filter(status=args["status"])
    apps = list(qs[: args["limit"]])
    return tool_ok({"applications": [application_out(a) for a in apps]}, f"{len(apps)} application(s).")


@register("get_application", scope="applications:read",
          description="One application with its draft content and attached documents.",
          input_schema={"type": "object", "properties": {"application_id": {"type": "integer"}},
                        "required": ["application_id"], "additionalProperties": False})
def get_application(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(ApplicationIdArgs, arguments)
    app = own_application(ctx, args["application_id"])
    return tool_ok(application_out(app), f"Application {app.id}: {app.status}")


EDITABLE = (ApplicationStatus.PENDING, ApplicationStatus.REVIEW)


def _audit(ctx: ToolContext, action: str, app: Application, **extra) -> None:
    AuditLog.objects.record(
        action=f"mcp.{action}", org=ctx.org, actor=ctx.user, target=app,
        payload={"prefix": ctx.token.prefix, **extra}, ip=ctx.ip,
    )


def _resolve_documents(ctx: ToolContext, ids: list[int]) -> list[Document]:
    if not ids:
        return []
    found = {d.id: d for d in Document.objects.for_org(ctx.org).filter(user=ctx.user, pk__in=ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise ToolError("document_not_found", f"Unknown document id(s): {missing}.")
    return [found[i] for i in ids]


def _set_documents(app: Application, docs: list[Document]) -> None:
    ApplicationDocument.objects.filter(application=app).delete()
    ApplicationDocument.objects.bulk_create(
        [ApplicationDocument(application=app, document=d, position=i) for i, d in enumerate(docs)]
    )


class DraftArgs(serializers.Serializer):
    job_id = serializers.IntegerField(min_value=1)
    subject = serializers.CharField(max_length=255)
    content = serializers.CharField()
    cover_letter = serializers.CharField(required=False, allow_blank=True, default="")
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    document_ids = serializers.ListField(child=serializers.IntegerField(min_value=1),
                                         required=False, default=list)

    def validate_document_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("document_ids must not contain duplicates.")
        return value


class UpdateArgs(serializers.Serializer):
    application_id = serializers.IntegerField(min_value=1)
    subject = serializers.CharField(max_length=255, required=False)
    content = serializers.CharField(required=False)
    cover_letter = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    document_ids = serializers.ListField(child=serializers.IntegerField(min_value=1), required=False)

    def validate_document_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError("document_ids must not contain duplicates.")
        return value

    def validate(self, attrs):
        if set(attrs) == {"application_id"}:
            raise serializers.ValidationError({"fields": "Provide at least one field to change."})
        return attrs


_DRAFT_SCHEMA = {"type": "object", "properties": {
    "job_id": {"type": "integer"}, "subject": {"type": "string", "maxLength": 255},
    "content": {"type": "string", "description": "The application email / message body."},
    "cover_letter": {"type": "string"}, "notes": {"type": "string"},
    "document_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["job_id", "subject", "content"], "additionalProperties": False}


@register("draft_application", scope="applications:write",
          description="Create an application draft in REVIEW for a job. The assistant writes the "
                      "content; nothing is sent. Fails with application_exists if one exists.",
          input_schema=_DRAFT_SCHEMA)
def draft_application(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(DraftArgs, arguments)
    job = JobListing.objects.select_related("platform").filter(pk=args["job_id"]).first()
    if job is None:
        raise ToolError("job_not_found", "No such job listing.")
    existing = own_applications(ctx).filter(job_listing=job).first()
    if existing is not None:
        raise ToolError("application_exists", f"Application {existing.id} already exists for this job.",
                        extra={"application_id": existing.id})
    docs = _resolve_documents(ctx, args["document_ids"])
    app = Application.objects.create(
        org=ctx.org, user=ctx.user, job_listing=job, status=ApplicationStatus.REVIEW,
        subject=args["subject"], generated_content=args["content"],
        generated_cover_letter=args["cover_letter"], notes=args["notes"],
    )
    _set_documents(app, docs)
    _audit(ctx, "draft_application", app, job_id=job.id)
    app = own_application(ctx, app.id)
    return tool_ok(application_out(app), f"Draft {app.id} created in REVIEW.")


_UPDATE_SCHEMA = {"type": "object", "properties": {
    "application_id": {"type": "integer"}, "subject": {"type": "string", "maxLength": 255},
    "content": {"type": "string"}, "cover_letter": {"type": "string"}, "notes": {"type": "string"},
    "document_ids": {"type": "array", "items": {"type": "integer"}}},
    "required": ["application_id"], "additionalProperties": False}


@register("update_application", scope="applications:write",
          description="Edit a draft (PENDING/REVIEW only). document_ids replaces the whole set.",
          input_schema=_UPDATE_SCHEMA)
def update_application(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(UpdateArgs, arguments)
    app = own_application(ctx, args["application_id"])
    if app.status not in EDITABLE:
        raise ToolError("application_locked", f"Application is {app.status}; only PENDING/REVIEW drafts can be edited.")
    changed = []
    for arg, field in (("subject", "subject"), ("content", "generated_content"),
                       ("cover_letter", "generated_cover_letter"), ("notes", "notes")):
        if arg in args:
            setattr(app, field, args[arg])
            changed.append(field)
    if changed:
        app.save(update_fields=changed + ["updated_at"])
    if "document_ids" in args:
        _set_documents(app, _resolve_documents(ctx, args["document_ids"]))
        changed.append("documents")
    _audit(ctx, "update_application", app, fields=changed)
    app = own_application(ctx, app.id)
    return tool_ok(application_out(app), f"Application {app.id} updated.")
