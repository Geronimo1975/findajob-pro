from __future__ import annotations

from django.core.files.base import ContentFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from apps.jobs.models import Application, ApplicationStatus, Document
from apps.tenancy.models import AuditLog
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_api import seed_listing, seed_platform
from tests.support import make_user
from tests.tenancy.mcp.support import call_tool, make_token


def make_document(org, user, name="cv.pdf") -> Document:
    doc = Document(org=org, user=user, name=name, kind="cv")
    doc.file.save(name, ContentFile(b"%PDF-1.4"), save=False)
    doc.save()
    return doc


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


class DraftApplicationTests(ApplicationToolsBase):
    def test_creates_in_review_with_documents_and_audit(self):
        doc = make_document(self.org, self.user)
        sc = self.sc("draft_application", job_id=self.low_job.id, subject="Application",
                     content="Dear team", cover_letter="CL", notes="n", document_ids=[doc.id])
        self.assertEqual(sc["status"], "review")
        self.assertEqual(sc["documents"], [{"id": doc.id, "name": "cv.pdf", "kind": "cv"}])
        app = Application.objects.for_org(self.org).get()
        self.assertEqual(app.generated_content, "Dear team")
        actions = list(AuditLog.objects.for_org(self.org).values_list("action", flat=True))
        self.assertEqual(actions, ["mcp.draft_application"])
        payload = AuditLog.objects.for_org(self.org).get().payload
        self.assertEqual(payload["prefix"], self.token.prefix)

    def test_duplicate_is_application_exists(self):
        self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c")
        sc = self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c")
        self.assertEqual(sc["error"]["code"], "application_exists")
        self.assertEqual(sc["error"]["application_id"], Application.objects.for_org(self.org).get().id)

    def test_unknown_job(self):
        self.assertEqual(self.sc("draft_application", job_id=99999, subject="s", content="c")["error"]["code"],
                         "job_not_found")

    def test_foreign_document_is_document_not_found_and_nothing_created(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        foreign = make_document(other_org, other)
        sc = self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c",
                     document_ids=[foreign.id])
        self.assertEqual(sc["error"]["code"], "document_not_found")
        self.assertEqual(Application.all_objects.count(), 0)

    def test_missing_content_is_invalid_arguments(self):
        self.assertEqual(self.sc("draft_application", job_id=self.low_job.id)["error"]["code"],
                         "invalid_arguments")

    def test_duplicate_document_ids_is_invalid_arguments_and_nothing_created(self):
        doc = make_document(self.org, self.user)
        sc = self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c",
                     document_ids=[doc.id, doc.id])
        self.assertEqual(sc["error"]["code"], "invalid_arguments")
        self.assertEqual(Application.all_objects.count(), 0)


class UpdateApplicationTests(ApplicationToolsBase):
    def _draft(self):
        return self.sc("draft_application", job_id=self.low_job.id, subject="s", content="c")["id"]

    def test_partial_update_in_review(self):
        app_id = self._draft()
        sc = self.sc("update_application", application_id=app_id, content="new body")
        self.assertEqual(sc["content"], "new body")
        self.assertEqual(sc["subject"], "s")

    def test_replaces_document_set(self):
        app_id = self._draft()
        d1, d2 = make_document(self.org, self.user, "a.pdf"), make_document(self.org, self.user, "b.pdf")
        self.sc("update_application", application_id=app_id, document_ids=[d1.id, d2.id])
        sc = self.sc("update_application", application_id=app_id, document_ids=[d2.id])
        self.assertEqual([d["id"] for d in sc["documents"]], [d2.id])

    def test_locked_after_sent(self):
        app_id = self._draft()
        Application.all_objects.filter(pk=app_id).update(status=ApplicationStatus.SENT)
        self.assertEqual(self.sc("update_application", application_id=app_id, content="x")["error"]["code"],
                         "application_locked")

    def test_no_fields_is_invalid_arguments(self):
        app_id = self._draft()
        self.assertEqual(self.sc("update_application", application_id=app_id)["error"]["code"],
                         "invalid_arguments")
