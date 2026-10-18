"""Command-center data for the dashboard index: KPIs and feeds."""

from __future__ import annotations

from django.db.models import Count

from apps.dashboard.automation import ingestion_runs
from apps.jobs.matching import skill_match
from apps.jobs.models import (
    Application, ApplicationStatus as S, Document, DocumentKind, JobListing,
)
from apps.profiles.services import get_or_create_search_profile

IN_PROGRESS = (S.PENDING, S.RUNNING, S.REVIEW, S.SENDING)
TERMINAL = (S.REJECTED, S.OFFER)


def build_command_center(org, user, limit: int = 10) -> dict:
    if org is None:
        return {
            "kpis": {"sent": 0, "in_progress": 0, "interviews": 0},
            "recent_jobs": [],
            "active_applications": [],
            "documents_summary": [],
            "job_rows": [],
            "recommendations": [],
            "ingestion_runs": [],
            "min_prescore": None,
        }
    profile = get_or_create_search_profile(org, user)
    listings = list(
        JobListing.objects.select_related("platform").order_by("-scraped_at", "-id")[:limit]
    )
    job_rows = sorted(
        ({"listing": jl, "match": skill_match(jl, profile)} for jl in listings),
        key=lambda r: r["match"]["score"], reverse=True,
    )
    labels = dict(DocumentKind.choices)
    doc_counts = (
        Document.objects.for_org(org).for_user(user).filter(user=user)
        .values("kind").annotate(n=Count("id")).order_by("kind")
    )
    apps = Application.objects.for_org(org).for_user(user).filter(user=user)
    app_status = {
        a.job_listing_id: a.get_status_display() for a in apps.order_by("created_at")
    }
    for row in job_rows:
        row["app_status"] = app_status.get(row["listing"].id)
    return {
        "kpis": {
            "sent": apps.filter(status=S.SENT).count(),
            "in_progress": apps.filter(status__in=IN_PROGRESS).count(),
            "interviews": apps.filter(status=S.INTERVIEW).count(),
        },
        "recent_jobs": listings,
        "documents_summary": [
            {"kind": d["kind"], "label": str(labels.get(d["kind"], d["kind"])),
             "count": d["n"]}
            for d in doc_counts
        ],
        "job_rows": job_rows,
        "recommendations": [
            r for r in job_rows if r["match"]["score"] >= profile.min_prescore
        ][:5],
        "ingestion_runs": ingestion_runs(),
        "min_prescore": profile.min_prescore,
        "active_applications": list(
            apps.exclude(status__in=TERMINAL).select_related("job_listing")
        ),
    }
