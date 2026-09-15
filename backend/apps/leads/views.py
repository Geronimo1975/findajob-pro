"""``POST /api/v1/leads/demo/`` — the B2B "Demo anfragen" form on ``web/unternehmen.html``.

Anonymous, ``@csrf_protect`` for the same reason as ``request-code`` (DRF's
``APIView`` is ``csrf_exempt``; see ``apps.accounts.views``). No model: a lead
is mailed to ``settings.ADMINS`` synchronously inside the request, within the
same ``EMAIL_TIMEOUT`` budget the OTP mail uses, and the operator's mailbox is
the ledger. If nobody is configured to receive it the endpoint answers ``503``
rather than swallowing the lead — a demo request that vanishes is worse than
one the visitor is told to send by email instead.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import get_connection, mail_admins
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.ratelimit import check_request_budgets
from apps.accounts.services import client_ip
from apps.common.errors import ApiError, DeliveryUnavailable, ErrorCode, RateLimited

logger = logging.getLogger("findajob.leads")

SEGMENTS = ("recruiting", "coaching", "bootcamp", "outplacement", "whitelabel", "other")


class DemoLeadSerializer(serializers.Serializer):
    company = serializers.CharField(max_length=120)
    email = serializers.EmailField()
    segment = serializers.ChoiceField(choices=SEGMENTS)
    message = serializers.CharField(max_length=2000, required=False, allow_blank=True)
    # Honeypot: real browsers leave it empty; bots fill every field.
    website = serializers.CharField(required=False, allow_blank=True, max_length=200)


@method_decorator(csrf_protect, name="dispatch")
class DemoLeadView(APIView):
    authentication_classes: list = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = DemoLeadSerializer(data=request.data)
        if not serializer.is_valid():
            code = ErrorCode.INVALID_EMAIL if "email" in serializer.errors else ErrorCode.INVALID
            raise ApiError(code, "Please check the form fields.", 400)
        data = serializer.validated_data

        if data.get("website"):
            # Honeypot tripped: same 202 as success, mail nothing.
            return Response({"detail": "accepted"}, status=status.HTTP_202_ACCEPTED)

        # ponytail: shares the OTP budgets (5/email/h, 20/ip/h, 500/h global);
        # give leads their own keys if a prospect ever hits the login limit.
        decision = check_request_budgets(data["email"], client_ip(request))
        if not decision.allowed:
            raise RateLimited(decision.retry_after)

        if not settings.ADMINS:
            logger.error("Demo lead received but DJANGO_ADMINS is empty; refusing.")
            raise DeliveryUnavailable()

        body = (
            f"Company:  {data['company']}\n"
            f"Email:    {data['email']}\n"
            f"Segment:  {data['segment']}\n"
            f"IP:       {client_ip(request) or '-'}\n\n"
            f"{data.get('message', '')}\n"
        )
        try:
            connection = get_connection(fail_silently=False, timeout=settings.EMAIL_TIMEOUT)
            mail_admins(
                f"Demo-Anfrage: {data['company']} ({data['segment']})",
                body,
                fail_silently=False,
                connection=connection,
            )
        except Exception as exc:  # noqa: BLE001 - every ESP raises its own hierarchy
            logger.error("Demo lead delivery failed: %s", exc.__class__.__name__, exc_info=True)
            raise DeliveryUnavailable() from exc

        logger.info("Demo lead accepted for segment=%s", data["segment"])
        return Response({"detail": "accepted"}, status=status.HTTP_202_ACCEPTED)
