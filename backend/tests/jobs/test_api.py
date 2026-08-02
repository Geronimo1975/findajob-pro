"""``GET /api/v1/platforms/`` and ``GET /api/v1/jobs/`` — the dashboard reads.

The wire shape is pinned to ``web/api.js`` fixtures (architecture rule 3):
the frontend swaps fixture bodies for these responses with no key changes.
The load-bearing behaviours:

- a brand-new account gets ``[]`` from ``/jobs/`` — never sample data;
- application state (``status``/``sent_manually``/``sent_at``) is the calling
  member's own, never another tenant's;
- both endpoints are session-gated (401 anonymous).
"""

from __future__ import annotations

from django.test import TestCase
from django.utils import timezone

from apps.jobs.models import Application, ApplicationStatus, JobListing, Platform
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user

PLATFORMS_URL = "/api/v1/platforms/"
JOBS_URL = "/api/v1/jobs/"


def seed_platform(slug="remoteok", **overrides) -> Platform:
    defaults = dict(
        name="RemoteOK",
        connector_type="api",
        auth_method="none",
        apply_mechanism="email",
        risk="low",
        auto_send=True,
        is_active=True,
        rate_limit_config={"daily": 60, "delay_seconds": 5},
    )
    defaults.update(overrides)
    return Platform.objects.create(slug=slug, **defaults)


def seed_listing(platform: Platform, **overrides) -> JobListing:
    defaults = dict(
        title="Senior Django Engineer",
        company="Loftware",
        url=f"https://example.com/{platform.slug}/{JobListing.objects.count() + 1}",
        tags=["Django", "DRF"],
        platform_specific_data={"apply_email": "jobs@example.com"},
        scraped_at=timezone.now(),
    )
    defaults.update(overrides)
    return JobListing.objects.create(platform=platform, **defaults)


class AnonymousTests(TestCase):
    def test_both_endpoints_are_401_for_anonymous(self):
        for url in (PLATFORMS_URL, JOBS_URL):
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.json()["error"]["code"], "not_authenticated"
                )


class PlatformListTests(TestCase):
    def setUp(self):
        self.user = make_user()
        bootstrap_workspace(self.user)
        self.client.force_login(self.user)

    def test_platform_wire_shape_matches_api_js(self):
        seed_platform()
        row = self.client.get(PLATFORMS_URL).json()[0]
        self.assertEqual(
            set(row),
            {"id", "name", "connector_type", "auth_method", "apply_mechanism",
             "risk", "auto_send", "is_active", "rate_limit", "note"},
        )
        self.assertEqual(row["id"], "remoteok")
        self.assertEqual(row["rate_limit"], "60/day, 5s delay")

    def test_feed_only_rate_limit_renders_like_the_fixture(self):
        seed_platform(slug="indeed", name="Indeed",
                      apply_mechanism="external_redirect", risk="high",
                      auto_send=False, is_active=False,
                      rate_limit_config={"feed_only": True})
        row = self.client.get(PLATFORMS_URL).json()[0]
        self.assertEqual(row["rate_limit"], "feed only")


class JobListTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.client.force_login(self.user)

    def test_new_account_gets_an_empty_list_not_samples(self):
        response = self.client.get(JOBS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_job_row_wire_shape_matches_api_js(self):
        listing = seed_listing(seed_platform())
        row = self.client.get(JOBS_URL).json()[0]
        self.assertEqual(
            set(row),
            {"id", "platform", "title", "company", "url", "location", "salary",
             "description", "requirements", "tags", "psd", "scraped_at",
             "status", "sent_manually", "sent_at"},
        )
        self.assertEqual(row["id"], listing.pk)
        self.assertEqual(row["platform"], "remoteok")
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["psd"], {"apply_email": "jobs@example.com"})
        self.assertIsInstance(row["scraped_at"], int)
        self.assertIsNone(row["sent_at"])

    def test_own_application_state_is_flattened_onto_the_row(self):
        listing = seed_listing(seed_platform())
        sent_at = timezone.now()
        Application.all_objects.create(
            org=self.org, user=self.user, job_listing=listing,
            status=ApplicationStatus.SENT, sent_at=sent_at,
        )
        row = self.client.get(JOBS_URL).json()[0]
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["sent_at"], int(sent_at.timestamp() * 1000))

    def test_another_members_application_never_leaks(self):
        listing = seed_listing(seed_platform())
        other = make_user()
        other_org = bootstrap_workspace(other).org
        Application.all_objects.create(
            org=other_org, user=other, job_listing=listing,
            status=ApplicationStatus.INTERVIEW,
        )
        row = self.client.get(JOBS_URL).json()[0]
        self.assertEqual(row["status"], "pending")

    def test_platform_filter(self):
        remoteok = seed_platform()
        wwr = seed_platform(slug="wwr", name="We Work Remotely",
                            connector_type="rss")
        seed_listing(remoteok, url="https://example.com/a")
        seed_listing(wwr, url="https://example.com/b", title="Python Dev")
        rows = self.client.get(JOBS_URL, {"platform": "wwr"}).json()
        self.assertEqual([r["platform"] for r in rows], ["wwr"])

    def test_status_filter_and_invalid_status(self):
        seed_listing(seed_platform())
        self.assertEqual(
            len(self.client.get(JOBS_URL, {"status": "sent"}).json()), 0
        )
        self.assertEqual(
            len(self.client.get(JOBS_URL, {"status": "pending"}).json()), 1
        )
        bad = self.client.get(JOBS_URL, {"status": "nope"})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["error"]["code"], "invalid_status")

    def test_inactive_platform_listings_are_hidden(self):
        inactive = seed_platform(slug="indeed", name="Indeed", is_active=False,
                                 auto_send=False, risk="high",
                                 apply_mechanism="external_redirect")
        seed_listing(inactive)
        self.assertEqual(self.client.get(JOBS_URL).json(), [])
