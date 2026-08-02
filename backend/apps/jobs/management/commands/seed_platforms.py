"""Seed the Platform catalog — the five launch platforms from the contract.

Mirrors ``prototype/api.js`` ``PLATFORMS`` with the audited field fixes
(``rate_limit`` display string → ``rate_limit_config`` JSON). Idempotent:
re-running updates in place, same as ``seed_legal``. The per-platform risk
rules encoded here are law (architecture spec): ``auto_send`` only on
low-risk email platforms — the model's CheckConstraint enforces it too.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.jobs.models import Platform

PLATFORMS = [
    {
        "slug": "linkedin", "name": "LinkedIn",
        "connector_type": "browser_automation", "auth_method": "cookie_session",
        "apply_mechanism": "in_platform_form", "risk": "high",
        "auto_send": False, "is_active": True,
        "rate_limit_config": {"daily": 10, "delay_seconds": 90},
        "note": "Semi-manual only. Submit is always a human click — "
                "automation violates the User Agreement.",
    },
    {
        "slug": "wwr", "name": "We Work Remotely",
        "connector_type": "rss", "auth_method": "none",
        "apply_mechanism": "email", "risk": "low",
        "auto_send": True, "is_active": True,
        "rate_limit_config": {"daily": 60, "delay_seconds": 5},
        "note": "Official RSS feed. Safe for full automation.",
    },
    {
        "slug": "remoteok", "name": "RemoteOK",
        "connector_type": "api", "auth_method": "none",
        "apply_mechanism": "email", "risk": "low",
        "auto_send": True, "is_active": True,
        "rate_limit_config": {"daily": 60, "delay_seconds": 5},
        "note": "Official JSON API. Safe for full automation.",
    },
    {
        "slug": "wellfound", "name": "Wellfound",
        "connector_type": "scraper_html", "auth_method": "login_form",
        "apply_mechanism": "message_to_founder", "risk": "medium",
        "auto_send": False, "is_active": True,
        "rate_limit_config": {"daily": 15, "delay_seconds": 60},
        "note": "Founder messages need mandatory human review before send.",
    },
    {
        "slug": "indeed", "name": "Indeed",
        "connector_type": "api", "auth_method": "api_key",
        "apply_mechanism": "external_redirect", "risk": "high",
        "auto_send": False, "is_active": False,
        "rate_limit_config": {"feed_only": True},
        "note": "Scraping prohibited. Publisher XML feed or manual paste only.",
    },
]


class Command(BaseCommand):
    help = "Seed or update the five launch platforms."

    def handle(self, *args, **options):
        created = updated = 0
        for row in PLATFORMS:
            _, was_created = Platform.objects.update_or_create(
                slug=row["slug"], defaults={k: v for k, v in row.items() if k != "slug"}
            )
            created += was_created
            updated += not was_created
        self.stdout.write(
            self.style.SUCCESS(f"platforms: {created} created, {updated} updated")
        )
