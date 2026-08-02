"""Outbox views — ``/api/v1/outbox/mailbox/`` (outbox spec, "Endpoints").

Everything here is per-member-per-org: the mailbox addressed is always the
one belonging to the requesting user in the request's resolved workspace.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.errors import ApiError
from apps.outbox import crypto
from apps.outbox.models import MailboxConnection, MailboxStatus
from apps.outbox.serializers import ConnectMailboxSerializer, MailboxSerializer
from apps.outbox.services import SmtpCheckError, send_test_email, verify_smtp
from apps.tenancy.permissions import IsOrgMember
from apps.tenancy.services import current_org


class MailboxNotConnected(ApiError):
    status_code = status.HTTP_404_NOT_FOUND
    error_code = "mailbox_not_connected"
    default_detail = "No mailbox is connected in this workspace."


def _mailbox_for(request: Request) -> MailboxConnection | None:
    org = current_org(request)
    return (
        MailboxConnection.objects.for_org(org).filter(user=request.user).first()
    )


class MailboxView(APIView):
    """GET / POST / DELETE ``/api/v1/outbox/mailbox/``."""

    permission_classes = [IsOrgMember]

    def get(self, request: Request) -> Response:
        mailbox = _mailbox_for(request)
        if mailbox is None:
            raise MailboxNotConnected()
        return Response(MailboxSerializer(mailbox).data)

    def post(self, request: Request) -> Response:
        serializer = ConnectMailboxSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # Verify before saving anything: a failed login stores nothing.
        try:
            verify_smtp(data["smtp"])
        except SmtpCheckError as exc:
            raise ApiError(
                "smtp_verification_failed", exc.detail, status.HTTP_400_BAD_REQUEST
            ) from exc

        org = current_org(request)
        mailbox, _created = MailboxConnection.objects.for_org(org).update_or_create(
            org=org,
            user=request.user,
            defaults={
                "provider": data["provider"],
                "email_address": data["email_address"],
                "display_name": data.get("display_name", ""),
                "credentials_encrypted": crypto.encrypt_credentials(data["smtp"]),
                "status": MailboxStatus.CONNECTED,
                "last_verified_at": timezone.now(),
                "last_error_code": "",
            },
        )
        return Response(
            MailboxSerializer(mailbox).data, status=status.HTTP_201_CREATED
        )

    def delete(self, request: Request) -> Response:
        mailbox = _mailbox_for(request)
        if mailbox is None:
            raise MailboxNotConnected()
        # Delete the row outright: secrets must not survive a disconnect.
        mailbox.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class MailboxTestView(APIView):
    """POST ``/api/v1/outbox/mailbox/test/`` — mail the user's own address."""

    permission_classes = [IsOrgMember]

    def post(self, request: Request) -> Response:
        mailbox = _mailbox_for(request)
        if mailbox is None:
            raise MailboxNotConnected()
        credentials = crypto.decrypt_credentials(mailbox.credentials_encrypted)
        try:
            send_test_email(
                credentials,
                from_address=mailbox.email_address,
                display_name=mailbox.display_name,
            )
        except SmtpCheckError as exc:
            mailbox.status = MailboxStatus.ERROR
            mailbox.last_error_code = exc.code
            mailbox.save(update_fields=["status", "last_error_code", "updated_at"])
            raise ApiError(
                "send_failed", exc.detail, status.HTTP_502_BAD_GATEWAY
            ) from exc
        mailbox.status = MailboxStatus.CONNECTED
        mailbox.last_verified_at = timezone.now()
        mailbox.last_error_code = ""
        mailbox.save(
            update_fields=[
                "status", "last_verified_at", "last_error_code", "updated_at"
            ]
        )
        return Response(status=status.HTTP_202_ACCEPTED)
