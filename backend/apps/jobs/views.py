"""Jobs views — the two read endpoints the dashboard boots against.

    GET /api/v1/platforms/   session   the adapter catalog (global)
    GET /api/v1/jobs/        session   listings + the member's application state

A brand-new account gets ``[]`` from ``/jobs/`` until ingestion runs — that is
the point: real accounts see real (initially empty) data; only the anonymous
demo shell renders fixtures. Failures use the project envelope via the shared
exception handler; nothing here raises bespoke shapes.
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.errors import ApiError
from apps.jobs.models import Application, ApplicationStatus, JobListing, Platform
from apps.jobs.serializers import JobRowSerializer, PlatformSerializer
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
