"""Application tools (spec §5). Every read starts from
``Application.objects.for_org(ctx.org).filter(user=ctx.user)``."""
from __future__ import annotations

from rest_framework import serializers

from django.db.models import Prefetch

from apps.jobs.models import Application, ApplicationDocument, ApplicationStatus
from apps.tenancy.mcp.serializers import application_out
from apps.tenancy.mcp.tools import ToolContext, ToolError, register, tool_ok, validate

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
