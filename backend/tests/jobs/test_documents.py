"""``/api/v1/documents/`` — upload/list/download/delete.

``/media/`` is never served by nginx (``deploy/nginx.conf``) — reading a
document's bytes must go through this app's own authenticated view, which is
what the download tests below pin.
"""

from __future__ import annotations

import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings

from apps.jobs.models import Document, DocumentKind
from apps.tenancy.managers import OrgScopeError
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user

DOCUMENTS_URL = "/api/v1/documents/"

_MEDIA_ROOT = tempfile.mkdtemp(prefix="findajob-test-media-")


def _pdf(name: str = "CV — Backend (EN).pdf", size: int = 2048) -> SimpleUploadedFile:
    return SimpleUploadedFile(
        name, b"%PDF-1.4 " + b"x" * size, content_type="application/pdf"
    )


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class DocumentApiTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA_ROOT, ignore_errors=True)

    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.client.force_login(self.user)

    def test_anonymous_is_401(self):
        self.client.logout()
        response = self.client.get(DOCUMENTS_URL)
        self.assertEqual(response.status_code, 401)

    def test_new_account_gets_empty_list(self):
        response = self.client.get(DOCUMENTS_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_another_members_documents_never_leak(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        Document.all_objects.create(
            org=other_org, user=other, name="Other CV.pdf", kind=DocumentKind.CV,
            file=_pdf(), size_bytes=2048,
        )
        self.assertEqual(self.client.get(DOCUMENTS_URL).json(), [])

    def test_upload_returns_the_fixture_shape(self):
        response = self.client.post(DOCUMENTS_URL, {"file": _pdf("CV — Backend (EN).pdf")})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(
            set(body), {"id", "name", "kind", "tags", "size", "updated", "ats_score"}
        )
        self.assertEqual(body["name"], "CV — Backend (EN).pdf")
        self.assertEqual(body["kind"], "cv")
        self.assertIsNone(body["ats_score"])
        self.assertTrue(body["size"].endswith("KB"))

    def test_upload_persists_ownership_and_size(self):
        self.client.post(DOCUMENTS_URL, {"file": _pdf(size=4096)})
        document = Document.objects.for_org(self.org).get(user=self.user)
        self.assertEqual(document.org_id, self.org.pk)
        self.assertEqual(document.user_id, self.user.pk)
        self.assertGreaterEqual(document.size_bytes, 4096)

    def test_kind_is_inferred_from_filename_when_omitted(self):
        response = self.client.post(DOCUMENTS_URL, {"file": _pdf("AWS Certificate.pdf")})
        self.assertEqual(response.json()["kind"], "certificate")

    def test_explicit_kind_overrides_the_heuristic(self):
        response = self.client.post(
            DOCUMENTS_URL, {"file": _pdf("random-name.pdf"), "kind": "diploma"}
        )
        self.assertEqual(response.json()["kind"], "diploma")

    def test_invalid_kind_is_rejected(self):
        response = self.client.post(
            DOCUMENTS_URL, {"file": _pdf(), "kind": "not-a-real-kind"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_kind")

    def test_unsupported_extension_is_rejected(self):
        upload = SimpleUploadedFile(
            "script.exe", b"MZ", content_type="application/octet-stream"
        )
        response = self.client.post(DOCUMENTS_URL, {"file": upload})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "unsupported_document_type")

    def test_oversized_file_is_rejected(self):
        upload = _pdf(size=11 * 1024 * 1024)
        response = self.client.post(DOCUMENTS_URL, {"file": upload})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "document_too_large")

    def test_download_streams_the_owners_file(self):
        upload_response = self.client.post(DOCUMENTS_URL, {"file": _pdf()})
        doc_id = upload_response.json()["id"]
        response = self.client.get(f"{DOCUMENTS_URL}{doc_id}/download/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"%PDF-1.4", b"".join(response.streaming_content))

    def test_download_404s_for_another_members_document(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        other_doc = Document.all_objects.create(
            org=other_org, user=other, name="Other.pdf", kind=DocumentKind.CV,
            file=_pdf(), size_bytes=2048,
        )
        response = self.client.get(f"{DOCUMENTS_URL}{other_doc.pk}/download/")
        self.assertEqual(response.status_code, 404)

    def test_download_anonymous_is_401(self):
        upload_response = self.client.post(DOCUMENTS_URL, {"file": _pdf()})
        doc_id = upload_response.json()["id"]
        self.client.logout()
        response = self.client.get(f"{DOCUMENTS_URL}{doc_id}/download/")
        self.assertEqual(response.status_code, 401)

    def test_delete_removes_row_and_file(self):
        upload_response = self.client.post(DOCUMENTS_URL, {"file": _pdf()})
        doc_id = upload_response.json()["id"]
        document = Document.objects.for_org(self.org).get(pk=doc_id)
        self.assertTrue(document.file.storage.exists(document.file.name))

        response = self.client.delete(f"{DOCUMENTS_URL}{doc_id}/")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Document.all_objects.filter(pk=doc_id).exists())
        self.assertFalse(document.file.storage.exists(document.file.name))

    def test_delete_404s_for_another_members_document(self):
        other = make_user()
        other_org = bootstrap_workspace(other).org
        other_doc = Document.all_objects.create(
            org=other_org, user=other, name="Other.pdf", kind=DocumentKind.CV,
            file=_pdf(), size_bytes=2048,
        )
        response = self.client.delete(f"{DOCUMENTS_URL}{other_doc.pk}/")
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Document.all_objects.filter(pk=other_doc.pk).exists())

    def test_unscoped_query_still_raises(self):
        with self.assertRaises(OrgScopeError):
            list(Document.objects.all())
