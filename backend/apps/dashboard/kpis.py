"""Command-center data for the dashboard index: KPIs and feeds."""

from __future__ import annotations

from apps.jobs.models import Application, ApplicationStatus as S, JobListing

IN_PROGRESS = (S.PENDING, S.RUNNING, S.REVIEW, S.SENDING)
TERMINAL = (S.REJECTED, S.OFFER)


def build_command_center(org, user, limit: int = 10) -> dict:
    if org is None:
        return {
            "kpis": {"sent": 0, "in_progress": 0, "interviews": 0},
            "recent_jobs": [],
            "active_applications": [],
        }
    apps = Application.objects.for_org(org).for_user(user)
    return {
        "kpis": {
            "sent": apps.filter(status=S.SENT).count(),
            "in_progress": apps.filter(status__in=IN_PROGRESS).count(),
            "interviews": apps.filter(status=S.INTERVIEW).count(),
        },
        "recent_jobs": list(JobListing.objects.all()[:limit]),
        "active_applications": list(
            apps.exclude(status__in=TERMINAL).select_related("job_listing")
        ),
    }
