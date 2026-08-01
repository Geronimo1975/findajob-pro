"""Tenancy views — one endpoint.

Implementation spec §3::

    | GET | /api/v1/me/ | session | **Owned by `apps/tenancy`** |

    There is no /auth/me/ and no /auth/session/. The single session endpoint is
    GET /api/v1/me/.

This is also the gate the shell boots against (spec §7): ``app.html`` calls it
on load and redirects to ``/auth.html?next=…`` on **401**. Returning 403 here
instead of 401 would leave a logged-out user staring at an empty dashboard, so
the project's authentication class exists purely to keep that status honest —
see ``apps.common.authentication``.
"""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.tenancy.serializers import MeSerializer
from apps.tenancy.services import memberships_for, resolve_membership


class MeView(APIView):
    """``GET /api/v1/me/`` — who am I, and in which workspace.

    Only ``IsAuthenticated``, deliberately **not** ``IsOrgMember``: a user who
    has just verified but has no workspace yet must still get a well-formed
    payload (with ``org: null``) rather than a 403 the shell cannot interpret.

    Accepts an optional ``?org=<id>`` (or ``X-FindaJob-Org`` header) to select
    among several workspaces; naming one the user does not belong to is a 403
    with code ``not_org_member``, never a silent fallback.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        user = request.user
        memberships = list(memberships_for(user))
        membership = resolve_membership(
            user,
            request.query_params.get("org")
            or request.META.get("HTTP_X_FINDAJOB_ORG"),
            memberships=memberships,
        )
        payload = MeSerializer(
            user,
            context={
                "request": request,
                "memberships": memberships,
                "membership": membership,
            },
        ).data
        return Response(payload)
