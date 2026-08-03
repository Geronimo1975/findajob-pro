"""Adapters + ``ingest_jobs`` — parsing, upsert idempotency, risk-table guard.

HTTP is mocked at the adapter's ``fetch`` seam; no test touches the network.
"""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from apps.jobs.adapters import ADAPTERS
from apps.jobs.adapters.remoteok import RemoteOKAdapter
from apps.jobs.adapters.wwr import WWRAdapter
from apps.jobs.models import JobListing, Platform
from apps.jobs.services import ingest_platform

REMOTEOK_PAYLOAD = [
    {"legal": "API terms…"},
    {
        "id": 999, "position": "Senior Django Engineer", "company": "Loftware",
        "url": "https://remoteok.com/jobs/999", "location": "EU",
        "salary_min": 140000, "salary_max": 170000,
        "tags": ["django", "python", ""],
        "description": "<p>Apply at <b>jobs@loftware.io</b></p>",
        "apply_url": "https://remoteok.com/jobs/999/apply",
    },
    {"id": 1000, "position": "", "company": "Ghost", "url": "https://remoteok.com/jobs/1000"},
]

WWR_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Doist: Python Developer</title>
    <link>https://weworkremotely.com/jobs/1</link>
    <guid>wwr-1</guid>
    <region>Anywhere</region>
    <category>python</category><category>backend</category>
    <description>&lt;p&gt;Write to work@doist.com&lt;/p&gt;</description>
  </item>
  <item><title>No Link Co: Role</title><link></link></item>
</channel></rss>"""


class FakeResponse:
    def __init__(self, *, json_data=None, content=b""):
        self._json = json_data
        self.content = content

    def json(self):
        return self._json


def seed(slug: str, **overrides) -> Platform:
    defaults = dict(
        name=slug, connector_type="api", auth_method="none",
        apply_mechanism="email", risk="low", auto_send=True, is_active=True,
        rate_limit_config={},
    )
    defaults.update(overrides)
    return Platform.objects.create(slug=slug, **defaults)


class RemoteOKParseTests(TestCase):
    def test_parses_listing_and_skips_legal_notice_and_empty_titles(self):
        jobs = RemoteOKAdapter().parse(FakeResponse(json_data=REMOTEOK_PAYLOAD))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.title, "Senior Django Engineer")
        self.assertEqual(job.salary, "$140k–170k")
        self.assertEqual(job.tags, ["django", "python"])
        self.assertEqual(job.platform_specific_data["apply_email"], "jobs@loftware.io")
        self.assertEqual(job.platform_specific_data["source_id"], "999")
        self.assertNotIn("<p>", job.description)


class WWRParseTests(TestCase):
    def test_parses_item_splitting_company_from_title(self):
        jobs = WWRAdapter().parse(FakeResponse(content=WWR_XML))
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual((job.company, job.title), ("Doist", "Python Developer"))
        self.assertEqual(job.location, "Anywhere")
        self.assertEqual(job.tags, ["python", "backend"])
        self.assertEqual(job.platform_specific_data["apply_email"], "work@doist.com")

    def test_refuses_a_feed_carrying_a_dtd(self):
        evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x "y">]><rss/>'
        with self.assertRaises(ValueError):
            WWRAdapter().parse(FakeResponse(content=evil))


class IngestServiceTests(TestCase):
    def test_upsert_is_idempotent(self):
        platform = seed("remoteok")
        with mock.patch.object(
            RemoteOKAdapter, "fetch",
            return_value=FakeResponse(json_data=REMOTEOK_PAYLOAD),
        ):
            first = ingest_platform(platform)
            second = ingest_platform(platform)
        self.assertEqual((first.created, first.updated), (1, 0))
        self.assertEqual((second.created, second.updated), (0, 1))
        self.assertEqual(JobListing.objects.count(), 1)

    def test_disabled_platform_is_not_fetched(self):
        platform = seed("remoteok", is_active=False, auto_send=False)
        with mock.patch.object(RemoteOKAdapter, "fetch") as fetch:
            result = ingest_platform(platform)
        fetch.assert_not_called()
        self.assertEqual(result.error, "platform disabled")

    def test_transport_error_is_reported_not_raised(self):
        platform = seed("remoteok")
        with mock.patch.object(RemoteOKAdapter, "fetch", side_effect=OSError("boom")):
            result = ingest_platform(platform)
        self.assertIn("boom", result.error)
        self.assertEqual(JobListing.objects.count(), 0)

    def test_high_risk_platforms_have_no_adapter(self):
        """The risk table is law: no adapter may exist for these slugs."""
        for slug in ("linkedin", "indeed", "wellfound"):
            self.assertNotIn(slug, ADAPTERS)


class IngestCommandTests(TestCase):
    def test_command_ingests_active_adapter_platforms(self):
        seed("remoteok")
        out = StringIO()
        with mock.patch.object(
            RemoteOKAdapter, "fetch",
            return_value=FakeResponse(json_data=REMOTEOK_PAYLOAD),
        ):
            call_command("ingest_jobs", stdout=out)
        self.assertIn("remoteok: 1 created", out.getvalue())

    def test_unknown_platform_slug_errors(self):
        with self.assertRaises(CommandError):
            call_command("ingest_jobs", "--platform", "nope")

    def test_all_failures_exit_nonzero(self):
        seed("remoteok")
        with mock.patch.object(RemoteOKAdapter, "fetch", side_effect=OSError("down")):
            with self.assertRaises(CommandError):
                call_command("ingest_jobs", stderr=StringIO())

    def test_limit_caps_rows(self):
        seed("wwr", connector_type="rss")
        many = WWR_XML  # single-item feed; limit 0 proves the cap is applied
        with mock.patch.object(
            WWRAdapter, "fetch", return_value=FakeResponse(content=many)
        ):
            call_command("ingest_jobs", "--limit", "0", stdout=StringIO())
        self.assertEqual(JobListing.objects.count(), 0)
