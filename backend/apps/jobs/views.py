"""Jobs views — the two read endpoints the dashboard boots against.

    GET /api/v1/platforms/   session   the adapter catalog (global)
    GET /api/v1/jobs/        session   listings + the member's application state

A brand-new account gets ``[]`` from ``/jobs/`` until ingestion runs — that is
the point: real accounts see real (initially empty) data; only the anonymous
demo shell renders fixtures. Failures use the project envelope via the shared
exception handler; nothing here raises bespoke shapes.
"""

from __future__ import annotations

import re
from pathlib import Path

from django.http import FileResponse
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.errors import ApiError
from apps.jobs.models import (
    Application, ApplicationStatus, Document, DocumentKind, JobListing, Platform,
)
from apps.jobs.serializers import DocumentSerializer, JobRowSerializer, PlatformSerializer
from apps.tenancy.permissions import IsOrgMember
from apps.tenancy.services import current_membership

#: Cap one page of listings; ingestion volumes stay tiny for the MVP and the
#: dashboard renders a scrolling table, not a paginator, so this is a guard
#: rail rather than a paging contract.
MAX_ROWS = 200

_VALID_STATUSES = {choice for choice, _ in ApplicationStatus.choices}


class PlatformListView(APIView):
    """The catalog is global (shared scrape pool) but still session-gated:
    the module list, risk notes and rate limits are product content, not
    public marketing."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        platforms = Platform.objects.order_by("slug")
        return Response(PlatformSerializer(platforms, many=True).data)


class JobListView(APIView):
    """Listings overlaid with the requesting member's application state.

    Filters mirror the ``ENDPOINTS`` comment (``?platform=&status=``); an
    unknown status is a 400 rather than a silently empty list. ``status``
    filtering happens after the overlay because ``pending`` is the default
    state of a listing the member has no application row for yet.
    """

    permission_classes = [IsOrgMember]

    def get(self, request: Request) -> Response:
        membership = current_membership(request)

        status_param = request.query_params.get("status") or None
        if status_param is not None and status_param not in _VALID_STATUSES:
            raise ApiError("invalid_status", "Unknown application status.", 400)

        listings = (
            JobListing.objects.select_related("platform")
            .filter(platform__is_active=True)
            .order_by("-scraped_at", "-id")
        )
        platform_param = request.query_params.get("platform") or None
        if platform_param:
            listings = listings.filter(platform__slug=platform_param)
        listings = list(listings[:MAX_ROWS])

        applications = {
            app.job_listing_id: app
            for app in Application.objects.for_org(membership.org)
            .filter(user=request.user, job_listing_id__in=[l.pk for l in listings])
        }
        rows = JobRowSerializer(
            listings, many=True, context={"applications": applications}
        ).data
        if status_param is not None:
            rows = [row for row in rows if row["status"] == status_param]
        return Response(rows)


#: Matches the ``accept`` attribute on the file input in ``web/app.html``.
DOCUMENT_ALLOWED_EXTENSIONS = (".pdf", ".doc", ".docx")
DOCUMENT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB — 2026-09-11 spec


def _infer_kind(filename: str) -> str:
    """Same heuristic ``web/app.html``'s client-side ``onUpload`` used, so
    behavior is unchanged for a client that omits the (optional) ``kind``
    field: cv/resume in the name -> CV, cert -> certificate, else other."""
    if re.search(r"cv|resume", filename, re.IGNORECASE):
        return DocumentKind.CV
    if re.search(r"cert", filename, re.IGNORECASE):
        return DocumentKind.CERTIFICATE
    return DocumentKind.OTHER


def _owned_document_or_404(request: Request, pk: int) -> Document:
    """Fetch a document, scoped to the requesting member, or raise a 404.

    Used by both the delete and download views. Re-checks ``user_id`` even
    though ``for_org()`` already guarantees it — cheap insurance against a
    future refactor accidentally querying personal documents unscoped.
    """
    membership = current_membership(request)
    try:
        document = Document.objects.for_org(membership.org).get(pk=pk)
    except Document.DoesNotExist as exc:
        raise ApiError("not_found", "Not found.", 404) from exc
    if document.user_id != request.user.id:
        raise ApiError("not_found", "Not found.", 404)
    return document


class DocumentListCreateView(APIView):
    """``GET`` lists the member's documents; ``POST`` uploads one.

        GET  /api/v1/documents/    session   list, newest-updated first
        POST /api/v1/documents/    session   multipart {file, kind?}

    ``/media/`` is never served by nginx (``deploy/nginx.conf``) — this view
    and ``DocumentDownloadView`` are the only ways a document's bytes move.
    """

    permission_classes = [IsOrgMember]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request: Request) -> Response:
        membership = current_membership(request)
        documents = (
            Document.objects.for_org(membership.org)
            .filter(user=request.user)
            .order_by("-updated_at", "-id")
        )
        return Response(DocumentSerializer(documents, many=True).data)

    def post(self, request: Request) -> Response:
        membership = current_membership(request)
        upload = request.FILES.get("file")
        if upload is None:
            raise ApiError("missing_file", "No file was uploaded.", 400)

        extension = Path(upload.name).suffix.lower()
        if extension not in DOCUMENT_ALLOWED_EXTENSIONS:
            raise ApiError(
                "unsupported_document_type",
                f"Only {', '.join(DOCUMENT_ALLOWED_EXTENSIONS)} files are accepted.",
                400,
            )
        if upload.size > DOCUMENT_MAX_UPLOAD_BYTES:
            raise ApiError(
                "document_too_large", "Files larger than 10 MB are not accepted.", 413
            )

        kind = request.data.get("kind") or _infer_kind(upload.name)
        if kind not in DocumentKind.values:
            raise ApiError("invalid_kind", "Unknown document kind.", 400)

        document = Document.objects.create(
            org=membership.org,
            user=request.user,
            name=upload.name,
            kind=kind,
            file=upload,
            size_bytes=upload.size,
        )
        return Response(DocumentSerializer(document).data, status=201)
