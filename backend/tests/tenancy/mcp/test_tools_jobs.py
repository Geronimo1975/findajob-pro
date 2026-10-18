from __future__ import annotations

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from apps.jobs.models import Application, ApplicationStatus
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_api import seed_listing, seed_platform
from tests.support import make_user
from tests.tenancy.mcp.support import call_tool, make_token


def result_of(response):
    return response.json()["result"]


class ListJobsTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.raw, _ = make_token(self.user, self.org)
        self.remoteok = seed_platform()
        self.linkedin = seed_platform("linkedin", name="LinkedIn", connector_type="browser_automation",
                                      apply_mechanism="in_platform_form", risk="high", auto_send=False)
        self.j1 = seed_listing(self.remoteok, title="Django Engineer")
        self.j2 = seed_listing(self.linkedin, title="Frontend Lead", company="Acme")

    def test_lists_with_platform_flags_and_null_application(self):
        r = result_of(call_tool(self.client, self.raw, "list_jobs"))
        self.assertFalse(r["isError"])
        jobs = r["structuredContent"]["jobs"]
        self.assertEqual({j["id"] for j in jobs}, {self.j1.id, self.j2.id})
        li = next(j for j in jobs if j["id"] == self.j2.id)
        self.assertEqual(li["platform"], {"slug": "linkedin", "name": "LinkedIn", "risk": "high",
                                          "apply_mechanism": "in_platform_form", "auto_send": False})
        self.assertIsNone(li["application_status"])

    def test_filters(self):
        sc = lambda **a: result_of(call_tool(self.client, self.raw, "list_jobs", a))["structuredContent"]
        self.assertEqual([j["id"] for j in sc(platform="linkedin")["jobs"]], [self.j2.id])
        self.assertEqual([j["id"] for j in sc(q="acme")["jobs"]], [self.j2.id])
        future = (timezone.now() + timedelta(days=1)).isoformat()
        self.assertEqual(sc(since=future)["jobs"], [])
        self.assertEqual(len(sc(limit=1)["jobs"]), 1)

    def test_limit_over_50_is_invalid_arguments(self):
        r = result_of(call_tool(self.client, self.raw, "list_jobs", {"limit": 51}))
        self.assertTrue(r["isError"])
        self.assertEqual(r["structuredContent"]["error"]["code"], "invalid_arguments")

    def test_overlay_is_own_application_only(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        Application.objects.create(org=other_org, user=other, job_listing=self.j1,
                                   status=ApplicationStatus.SENT)
        Application.objects.create(org=self.org, user=self.user, job_listing=self.j1,
                                   status=ApplicationStatus.REVIEW)
        jobs = result_of(call_tool(self.client, self.raw, "list_jobs"))["structuredContent"]["jobs"]
        mine = next(j for j in jobs if j["id"] == self.j1.id)
        self.assertEqual(mine["application_status"], "review")

    def test_token_is_pinned_to_its_org_not_the_users_other_orgs(self):
        # Same user, second workspace; the token belongs to org A only.
        from apps.tenancy.models import Membership, Organization, Role
        org_b = Organization.objects.create(name="B", slug="b-org")
        Membership.objects.create(org=org_b, user=self.user, role=Role.MEMBER)
        Application.objects.create(org=org_b, user=self.user, job_listing=self.j1,
                                   status=ApplicationStatus.SENT)
        jobs = result_of(call_tool(self.client, self.raw, "list_jobs"))["structuredContent"]["jobs"]
        self.assertIsNone(next(j for j in jobs if j["id"] == self.j1.id)["application_status"])

    def test_get_job_detail_and_not_found(self):
        r = result_of(call_tool(self.client, self.raw, "get_job", {"job_id": self.j1.id}))
        sc = r["structuredContent"]
        self.assertIn("description", sc)
        self.assertEqual(sc["platform_specific_data"], {"apply_email": "jobs@example.com"})
        r = result_of(call_tool(self.client, self.raw, "get_job", {"job_id": 999999}))
        self.assertEqual(r["structuredContent"]["error"]["code"], "job_not_found")
