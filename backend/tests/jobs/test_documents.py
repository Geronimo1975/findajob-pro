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
