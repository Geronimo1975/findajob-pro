"""Plain-dict wire shapes for tool results (spec §5)."""
from __future__ import annotations

from apps.jobs.models import Application, ApplicationDocument, JobListing


def _iso(dt):
    return dt.isoformat() if dt else None


def platform_out(platform) -> dict:
    return {"slug": platform.slug, "name": platform.name, "risk": platform.risk,
            "apply_mechanism": platform.apply_mechanism, "auto_send": platform.auto_send}


def job_summary(job: JobListing, application: Application | None) -> dict:
    return {
        "id": job.id, "platform": platform_out(job.platform), "title": job.title,
        "company": job.company, "url": job.url, "location": job.location,
        "salary": job.salary, "tags": job.tags or [], "scraped_at": _iso(job.scraped_at),
        "application_status": application.status if application else None,
        "application_id": application.id if application else None,
    }


def job_detail(job: JobListing, application: Application | None) -> dict:
    data = job_summary(job, application)
    data.update({"description": job.description, "requirements": job.requirements,
                 "platform_specific_data": job.platform_specific_data or {}})
    return data


def application_out(app: Application) -> dict:
    # Prefer the prefetched rows (own_applications() prefetches
    # ``applicationdocument_set``) so a list of N applications costs one extra
    # query, not N; falls back to a per-instance query for a lone ``app``
    # that was not built from that queryset (still scoped to this instance).
    if "applicationdocument_set" in getattr(app, "_prefetched_objects_cache", {}):
        rows = app.applicationdocument_set.all()
    else:
        rows = ApplicationDocument.objects.filter(application=app).select_related("document").order_by("position")
    docs = [{"id": ad.document.id, "name": ad.document.name, "kind": ad.document.kind} for ad in rows]
    return {
        "id": app.id, "job_id": app.job_listing_id, "job_title": app.job_listing.title,
        "company": app.job_listing.company, "platform_slug": app.job_listing.platform_id,
        "status": app.status, "subject": app.subject, "content": app.generated_content,
        "cover_letter": app.generated_cover_letter, "notes": app.notes, "documents": docs,
        "sent_manually": app.sent_manually, "sent_at": _iso(app.sent_at),
        "created_at": _iso(app.created_at), "updated_at": _iso(app.updated_at),
    }
