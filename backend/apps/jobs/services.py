"""Jobs services — ingestion, identical for every platform.

Adapters fetch and normalize; this layer persists. Upsert key is
``(platform, url)`` (the model's ``one_listing_per_platform_url``
constraint): a re-run refreshes listings in place and never duplicates a
row, so the command is safe on any schedule — cron, n8n, or by hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.utils import timezone

from apps.jobs.adapters import ADAPTERS
from apps.jobs.models import JobListing, Platform


@dataclass
class IngestResult:
    platform: str
    created: int = 0
    updated: int = 0
    skipped: int = 0
    error: str | None = None


def ingest_platform(platform: Platform, *, limit: int | None = None) -> IngestResult:
    """Fetch one platform's feed and upsert its listings.

    Returns a result object rather than raising on transport errors, so a
    multi-platform run reports one broken feed without abandoning the rest.
    """
    result = IngestResult(platform=platform.slug)
    adapter_cls = ADAPTERS.get(platform.slug)
    if adapter_cls is None:
        result.error = "no adapter (semi-manual platform)"
        return result
    if not platform.is_active:
        result.error = "platform disabled"
        return result

    try:
        jobs = adapter_cls().jobs()
    except Exception as exc:  # transport, JSON, XML — all end up here
        result.error = f"{type(exc).__name__}: {exc}"
        return result

    if limit is not None:
        jobs = jobs[:limit]
    now = timezone.now()
    for job in jobs:
        if not job.url:
            result.skipped += 1
            continue
        _, created = JobListing.objects.update_or_create(
            platform=platform,
            url=job.url,
            defaults={
                "title": job.title,
                "company": job.company,
                "location": job.location,
                "salary": job.salary,
                "description": job.description,
                "requirements": job.requirements,
                "tags": job.tags,
                "platform_specific_data": job.platform_specific_data,
                "scraped_at": now,
            },
        )
        result.created += created
        result.updated += not created
    return result
