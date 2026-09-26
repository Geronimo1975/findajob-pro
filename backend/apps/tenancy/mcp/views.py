"""``/mcp/`` — the MCP Streamable HTTP endpoint (spec §3).

A plain Django view on purpose: no DRF authentication classes, no session,
no CSRF. Bearer only (``auth.authenticate``). Failures before dispatch use the
project envelope; failures inside dispatch are JSON-RPC.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from apps.common.errors import ErrorCode, envelope
from apps.tenancy.mcp import rpc
from apps.tenancy.mcp.auth import McpAuthError, authenticate, client_ip
from apps.tenancy.mcp.ratelimit import McpRateLimited, check_budget
from apps.tenancy.mcp.tools import ToolContext

MAX_BODY_BYTES = 256 * 1024


def _envelope(status: int, code: str, detail: str, **headers: str) -> JsonResponse:
    response = JsonResponse(envelope(code, detail), status=status)
    for key, value in headers.items():
        response[key.replace("_", "-")] = value
    return response


@method_decorator(csrf_exempt, name="dispatch")
class McpEndpoint(View):
    http_method_names = ["post", "delete", "options"]

    def dispatch(self, request: HttpRequest, *args, **kwargs) -> HttpResponse:
        if not settings.MCP_SERVER_ENABLED:
            return _envelope(404, ErrorCode.NOT_FOUND, "Not found.")
        return super().dispatch(request, *args, **kwargs)

    def delete(self, request: HttpRequest) -> HttpResponse:
        try:
            authenticate(request)
        except McpAuthError as exc:
            return _envelope(401, exc.code, exc.detail, WWW_Authenticate="Bearer")
        return HttpResponse(status=204)

    def post(self, request: HttpRequest) -> HttpResponse:
        try:
            token, membership = authenticate(request)
        except McpAuthError as exc:
            return _envelope(401, exc.code, exc.detail, WWW_Authenticate="Bearer")
        if len(request.body) > MAX_BODY_BYTES:
            return _envelope(413, "payload_too_large", "Request body exceeds 256 KB.")
        try:
            check_budget(token.pk)
        except McpRateLimited as exc:
            return _envelope(429, ErrorCode.RATE_LIMITED,
                             "Too many requests. Please try again later.",
                             Retry_After=str(exc.retry_after))
        ctx = ToolContext(token=token, membership=membership, org=membership.org,
                          user=token.user, ip=client_ip(request))
        status, body = rpc.dispatch(request.body, ctx)
        if body is None:
            return HttpResponse(status=status)
        return JsonResponse(body, status=status)
