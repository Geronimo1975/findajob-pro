from __future__ import annotations

from django.test import TestCase

from apps.jobs.models import Application, ApplicationStatus
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_api import seed_listing, seed_platform
from tests.support import make_user
from tests.tenancy.mcp.support import call_tool, make_token


def result_of(response):
    return response.json()["result"]


class ApplicationToolsBase(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.raw, self.token = make_token(self.user, self.org)
        self.remoteok = seed_platform()
        self.linkedin = seed_platform("linkedin", name="LinkedIn", connector_type="browser_automation",
                                      apply_mechanism="in_platform_form", risk="high", auto_send=False)
        self.low_job = seed_listing(self.remoteok)
        self.high_job = seed_listing(self.linkedin, title="Lead")

    def call(self, name, **arguments):
        return result_of(call_tool(self.client, self.raw, name, arguments))

    def sc(self, name, **arguments):
        return self.call(name, **arguments)["structuredContent"]


class ReadApplicationTests(ApplicationToolsBase):
    def test_list_and_get_own_only(self):
        mine = Application.objects.create(org=self.org, user=self.user, job_listing=self.low_job,
                                          status=ApplicationStatus.REVIEW, subject="Hi")
        other = make_user()
        other_org = bootstrap_workspace(other).org
        theirs = Application.objects.create(org=other_org, user=other, job_listing=self.low_job)
        listed = self.sc("list_applications")["applications"]
        self.assertEqual([a["id"] for a in listed], [mine.id])
        self.assertEqual(self.sc("get_application", application_id=mine.id)["subject"], "Hi")
        self.assertEqual(self.sc("get_application", application_id=theirs.id)["error"]["code"],
                         "application_not_found")

    def test_list_filters_by_status(self):
        Application.objects.create(org=self.org, user=self.user, job_listing=self.low_job,
                                   status=ApplicationStatus.SENT)
        self.assertEqual(self.sc("list_applications", status="review")["applications"], [])
        self.assertEqual(len(self.sc("list_applications", status="sent")["applications"]), 1)
        self.assertEqual(self.sc("list_applications", status="bogus")["error"]["code"], "invalid_arguments")
