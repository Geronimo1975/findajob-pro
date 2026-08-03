"""``ingest_jobs`` — fetch the low-risk feeds into the JobListing catalog.

    ./manage.py ingest_jobs                    # every active platform with an adapter
    ./manage.py ingest_jobs --platform wwr     # one platform
    ./manage.py ingest_jobs --limit 25         # cap rows per platform

Idempotent (upsert on platform+url). Platforms without an adapter — the
semi-manual, high-risk ones — are reported and skipped, never scraped.
Exit code is non-zero only when every attempted platform failed, so a cron
run with one flaky feed still counts as a success.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.jobs.adapters import ADAPTERS
from apps.jobs.models import Platform
from apps.jobs.services import ingest_platform


class Command(BaseCommand):
    help = "Fetch official feeds (RemoteOK, WWR, …) into the JobListing catalog."

    def add_arguments(self, parser):
        parser.add_argument("--platform", help="Only this platform slug.")
        parser.add_argument(
            "--limit", type=int, default=None,
            help="Maximum listings to upsert per platform.",
        )

    def handle(self, *args, **options):
        platforms = Platform.objects.order_by("slug")
        if options["platform"]:
            platforms = platforms.filter(slug=options["platform"])
            if not platforms.exists():
                raise CommandError(f"unknown platform {options['platform']!r}")
        else:
            platforms = platforms.filter(slug__in=ADAPTERS)

        results = [ingest_platform(p, limit=options["limit"]) for p in platforms]
        if not results:
            raise CommandError("no platforms to ingest — run seed_platforms first")

        failures = 0
        for r in results:
            if r.error:
                failures += 1
                self.stderr.write(self.style.WARNING(f"{r.platform}: {r.error}"))
            else:
                self.stdout.write(self.style.SUCCESS(
                    f"{r.platform}: {r.created} created, {r.updated} updated"
                    + (f", {r.skipped} skipped" if r.skipped else "")
                ))
        if failures == len(results):
            raise CommandError("every platform failed")
