from __future__ import annotations

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.jobs.models import Application, ApplicationStatus, Document
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

    def _application_with_one_document(self, job_listing):
        app = Application.objects.create(org=self.org, user=self.user, job_listing=job_listing)
        doc = Document.objects.create(org=self.org, user=self.user, name="cv.pdf", kind="cv",
                                      file=ContentFile(b"%PDF-1.4", name="cv.pdf"))
        app.documents.add(doc, through_defaults={"position": 0})
        return app

    @staticmethod
    def _jobs_app_query_count(ctx):
        # Auth overhead (token lookup, membership, the occasional throttled
        # ``last_used_at`` update) is incidental to this test and varies call
        # to call; isolate the two queries the handler itself issues (the
        # applications page, and the one prefetch for all of its documents).
        return sum(1 for q in ctx.captured_queries
                   if "jobs_application" in q["sql"])

    def test_list_applications_query_count_does_not_grow_with_page_size(self):
        self._application_with_one_document(self.low_job)
        with CaptureQueriesContext(connection) as ctx:
            listed = self.sc("list_applications")["applications"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(len(listed[0]["documents"]), 1)
        one_app_query_count = self._jobs_app_query_count(ctx)
        self.assertEqual(one_app_query_count, 2)

        job2 = seed_listing(self.remoteok, url="https://example.com/remoteok/extra")
        job3 = seed_listing(self.remoteok, url="https://example.com/remoteok/extra2")
        self._application_with_one_document(job2)
        self._application_with_one_document(job3)
        with CaptureQueriesContext(connection) as ctx:
            listed = self.sc("list_applications")["applications"]
        self.assertEqual(len(listed), 3)
        for row in listed:
            self.assertEqual(len(row["documents"]), 1)
        # The point: one extra query for the whole page of documents, not one
        # per application — the count must not grow with the number of rows.
        self.assertEqual(self._jobs_app_query_count(ctx), one_app_query_count)
