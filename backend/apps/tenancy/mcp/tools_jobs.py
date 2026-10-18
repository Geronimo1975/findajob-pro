"""list_jobs / get_job (spec §5). Listings are global; the overlay is the
token member's own application in the token's org, nothing else."""
from __future__ import annotations

from rest_framework import serializers

from apps.jobs.models import Application, JobListing
from apps.tenancy.mcp.serializers import job_detail, job_summary
from apps.tenancy.mcp.tools import ToolContext, ToolError, register, tool_ok, validate

MAX_LIMIT = 50


class ListJobsArgs(serializers.Serializer):
    platform = serializers.SlugField(required=False)
    q = serializers.CharField(required=False, max_length=100)
    since = serializers.DateTimeField(required=False)
    limit = serializers.IntegerField(required=False, min_value=1, max_value=MAX_LIMIT, default=20)


class GetJobArgs(serializers.Serializer):
    job_id = serializers.IntegerField(min_value=1)


def _overlay(ctx: ToolContext, job_ids):
    rows = Application.objects.for_org(ctx.org).filter(user=ctx.user, job_listing_id__in=list(job_ids))
    return {a.job_listing_id: a for a in rows}


@register("list_jobs", scope="jobs:read",
          description="List scraped job listings with this member's application status. "
                      "Filters: platform (slug), q (title/company), since (ISO datetime), limit (<=50).",
          input_schema={"type": "object", "properties": {
              "platform": {"type": "string"}, "q": {"type": "string"},
              "since": {"type": "string", "format": "date-time"},
              "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIMIT, "default": 20}},
              "additionalProperties": False})
def list_jobs(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(ListJobsArgs, arguments)
    qs = JobListing.objects.select_related("platform").filter(platform__is_active=True)
    if args.get("platform"):
        qs = qs.filter(platform__slug=args["platform"])
    if args.get("q"):
        from django.db.models import Q
        qs = qs.filter(Q(title__icontains=args["q"]) | Q(company__icontains=args["q"]))
    if args.get("since"):
        qs = qs.filter(scraped_at__gte=args["since"])
    jobs = list(qs.order_by("-scraped_at", "-id")[: args["limit"]])
    overlay = _overlay(ctx, (j.id for j in jobs))
    data = {"jobs": [job_summary(j, overlay.get(j.id)) for j in jobs]}
    return tool_ok(data, f"{len(jobs)} job(s).")


@register("get_job", scope="jobs:read",
          description="One job listing with full description and requirements.",
          input_schema={"type": "object", "properties": {"job_id": {"type": "integer"}},
                        "required": ["job_id"], "additionalProperties": False})
def get_job(ctx: ToolContext, arguments: dict) -> dict:
    args = validate(GetJobArgs, arguments)
    job = JobListing.objects.select_related("platform").filter(pk=args["job_id"]).first()
    if job is None:
        raise ToolError("job_not_found", "No such job listing.")
    overlay = _overlay(ctx, [job.id])
    return tool_ok(job_detail(job, overlay.get(job.id)), f"{job.title} at {job.company}")
