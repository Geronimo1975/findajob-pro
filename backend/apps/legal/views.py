"""Public legal documents + the session-authenticated consent API.

Endpoints owned by this module::

    GET    /api/v1/legal/                 anon    index of the current documents
    GET    /api/v1/legal/{doc}/?lang=     anon    one document, with sections
    GET    /api/v1/consent/               session current state + recent history
    POST   /api/v1/consent/               session grant/withdraw one opt-in
    DELETE /api/v1/consent/{type}/        session withdraw one opt-in

The two ``legal/`` routes are among the three anonymous surfaces spec §7 allows
(``auth/*``, ``legal/*``, ``i18n/*``); everything else inherits DRF's default
``IsAuthenticated``. They have to be anonymous: § 5 DDG requires the Impressum
to be reachable without registering, and the terms have to be readable *before*
the checkbox that accepts them.

CSRF: ``APIView.as_view()`` is ``csrf_exempt``, so the writing view carries an
explicit ``@csrf_protect`` exactly like the anonymous auth POSTs in spec §8.
``SessionAuthentication.enforce_csrf`` would already cover a cookie-authenticated
POST; the decorator is the belt to that pair of braces, and it survives someone
later adding a non-session authentication class.
"""

from __future__ import annotations

from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.errors import ApiError, ErrorCode
from apps.legal.models import (
    BINDING_LANGUAGE,
    DOC_ID_ORDER,
    LANG_ORDER,
    REVOCABLE_CONSENT_TYPES,
)
from apps.legal.request_meta import client_ip, client_user_agent
from apps.legal.serializers import (
    ConsentRecordSerializer,
    ConsentWriteSerializer,
    LegalDocumentSerializer,
    LegalDocumentSummarySerializer,
)
from apps.legal.services import (
    consent_history,
    current_consent,
    grant_consent,
    normalise_lang,
    resolve_document,
    revoke_consent,
)


class LegalIndexView(APIView):
    """``GET /api/v1/legal/`` — every current document, without its body text."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        requested = normalise_lang(request.query_params.get("lang"))
        documents = []
        for doc_id in DOC_ID_ORDER:
            document, resolved = resolve_document(doc_id, requested)
            if document is None:
                # A document that has never been seeded is simply absent from
                # the index rather than a null entry: the frontend iterates it.
                continue
            data = LegalDocumentSummarySerializer(document).data
            data["requested_lang"] = requested
            data["resolved_lang"] = resolved
            documents.append(data)
        return Response(
            {
                "lang": requested,
                "binding_lang": BINDING_LANGUAGE,
                "languages": list(LANG_ORDER),
                "documents": documents,
            }
        )


class LegalDocumentView(APIView):
    """``GET /api/v1/legal/{doc}/?lang=`` — one document with its sections."""

    authentication_classes: list = []
    permission_classes = [AllowAny]

    def get(self, request: Request, doc_id: str) -> Response:
        key = (doc_id or "").strip().lower()
        if key not in DOC_ID_ORDER:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                "Unknown legal document.",
                status.HTTP_404_NOT_FOUND,
            )
        requested = normalise_lang(request.query_params.get("lang"))
        document, _resolved = resolve_document(key, requested)
        if document is None:
            raise ApiError(
                ErrorCode.NOT_FOUND,
                "This document has not been published yet.",
                status.HTTP_404_NOT_FOUND,
            )
        serializer = LegalDocumentSerializer(
            document, context={"request": request, "requested_lang": requested}
        )
        return Response(serializer.data)


@method_decorator(csrf_protect, name="dispatch")
class ConsentView(APIView):
    """``GET`` the user's consent state, ``POST`` a new decision."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        latest = current_consent(request.user)
        return Response(
            {
                "consents": {
                    consent_type: ConsentRecordSerializer(record).data
                    for consent_type, record in latest.items()
                },
                "granted": sorted(
                    consent_type
                    for consent_type, record in latest.items()
                    if record.granted
                ),
                "revocable": list(REVOCABLE_CONSENT_TYPES),
                "history": ConsentRecordSerializer(
                    consent_history(request.user), many=True
                ).data,
            }
        )

    def post(self, request: Request) -> Response:
        serializer = ConsentWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        record = grant_consent(
            request.user,
            data["type"],
            granted=data["granted"],
            ip=client_ip(request),
            user_agent=client_user_agent(request),
            ui_language=data.get("lang") or getattr(request, "LANGUAGE_CODE", ""),
        )
        return Response(
            ConsentRecordSerializer(record).data, status=status.HTTP_201_CREATED
        )


@method_decorator(csrf_protect, name="dispatch")
class ConsentRevokeView(APIView):
    """``DELETE /api/v1/consent/{type}/`` — withdraw one opt-in.

    Nothing is deleted. A withdrawal appends a ``granted=False`` row, because a
    ledger that can forget a decision cannot evidence the decisions it still
    holds. The 204 is about the *consent* being gone, not the record.
    """

    permission_classes = [IsAuthenticated]

    def delete(self, request: Request, consent_type: str) -> Response:
        key = (consent_type or "").strip().lower()
        revoke_consent(
            request.user,
            key,
            ip=client_ip(request),
            user_agent=client_user_agent(request),
            ui_language=getattr(request, "LANGUAGE_CODE", ""),
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
