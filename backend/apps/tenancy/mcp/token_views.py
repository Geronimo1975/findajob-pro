"""``/api/v1/mcp-tokens/`` — session-authenticated management of MCP PATs."""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.common.errors import ApiError, ErrorCode
from apps.tenancy.mcp.auth import client_ip
from apps.tenancy.mcp.models import McpToken, generate_token
from apps.tenancy.mcp.token_serializers import CreateMcpTokenSerializer, McpTokenSerializer
from apps.tenancy.models import AuditLog
from apps.tenancy.permissions import IsOrgMember
from apps.tenancy.services import current_org


def _own_tokens(request: Request):
    return McpToken.objects.for_org(current_org(request)).filter(user=request.user)


class McpTokenListView(APIView):
    permission_classes = [IsOrgMember]

    def get(self, request: Request) -> Response:
        return Response(McpTokenSerializer(_own_tokens(request), many=True).data)

    def post(self, request: Request) -> Response:
        serializer = CreateMcpTokenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        raw, prefix, digest = generate_token()
        org = current_org(request)
        token = McpToken.objects.create(
            org=org, user=request.user, name=data["name"], prefix=prefix,
            token_hash=digest, scopes=data["scopes"],
            expires_at=timezone.now() + timedelta(days=data["expires_in_days"]),
        )
        AuditLog.objects.record(
            action="mcp_token.created", org=org, actor=request.user, target=token,
            payload={"prefix": prefix, "scopes": data["scopes"]},
            ip=client_ip(request),
        )
        body = McpTokenSerializer(token).data
        body["token"] = raw  # the one and only time the plaintext leaves the server
        return Response(body, status=status.HTTP_201_CREATED)


class McpTokenDetailView(APIView):
    permission_classes = [IsOrgMember]

    def delete(self, request: Request, token_id: int) -> Response:
        token = _own_tokens(request).filter(pk=token_id).first()
        if token is None:
            raise ApiError(ErrorCode.NOT_FOUND, "Token not found.", status.HTTP_404_NOT_FOUND)
        if token.revoked_at is None:
            token.revoked_at = timezone.now()
            token.save(update_fields=["revoked_at"])
            AuditLog.objects.record(
                action="mcp_token.revoked", org=token.org, actor=request.user,
                target=token, payload={"prefix": token.prefix},
                ip=client_ip(request),
            )
        return Response(status=status.HTTP_204_NO_CONTENT)
