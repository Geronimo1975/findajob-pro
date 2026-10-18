"""Ingestion-run summary for the dashboard, derived from JobListing data."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.jobs.models import Platform


def ingestion_runs(limit: int = 20) -> list[dict]:
    """One row per platform with listings: last scrape and 7-day volume."""
    since = timezone.now() - timedelta(days=7)
    rows = (
        Platform.objects.filter(listings__isnull=False)
        .annotate(
            last_scraped_at=Max("listings__scraped_at"),
            new_count=Count("listings", filter=Q(listings__scraped_at__gte=since)),
        )
        .order_by("-last_scraped_at", "slug")[:limit]
    )
    return [
        {
            "platform": p.name or p.slug,
            "last_scraped_at": p.last_scraped_at,
            "new_count": p.new_count,
        }
        for p in rows
    ]
