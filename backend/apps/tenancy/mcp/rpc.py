"""JSON-RPC 2.0 envelope for MCP Streamable HTTP, phase 1 (spec §3).

Single request per POST (no batches), plain JSON responses (no SSE).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from django.db import transaction

from apps.tenancy.mcp.tools import REGISTRY, ToolContext, ToolError, load_all, tool_error

logger = logging.getLogger("findajob.mcp")

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "findajob.pro", "version": "1"}

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _result(id_: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _initialize(params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
        "instructions": (
            "findajob.pro job pipeline for this member's workspace. Drafts go to "
            "REVIEW; sending is a separate explicit tool. LinkedIn, Indeed and "
            "Wellfound are never automated: use mark_sent_manually after the "
            "member has applied by hand."
        ),
    }


def _tools_list(params: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    load_all()
    return {"tools": [tool.descriptor() for tool in REGISTRY.values()]}


def _tools_call(params: dict[str, Any], ctx: ToolContext) -> dict[str, Any] | None:
    load_all()
    name = params.get("name")
    tool = REGISTRY.get(name) if isinstance(name, str) else None
    if tool is None:
        return None  # caller maps to INVALID_PARAMS
    if not ctx.token.has_scope(tool.scope):
        return tool_error("scope_missing", f"This token lacks the {tool.scope} scope.")
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return tool_error("invalid_arguments", "arguments must be an object.")
    try:
        with transaction.atomic():
            return tool.handler(ctx, arguments)
    except ToolError as exc:
        return tool_error(exc.code, exc.detail)
    except Exception:  # noqa: BLE001 - every tool failure must stay in-band
        logger.exception("MCP tool %s failed", name)
        return tool_error("server_error", "The tool failed unexpectedly.")


def dispatch(body: bytes, ctx: ToolContext) -> tuple[int, dict[str, Any] | None]:
    """Return ``(http_status, json_body_or_None)``."""
    try:
        message = json.loads(body)
    except ValueError:
        return 200, _error(None, PARSE_ERROR, "Parse error")
    if not isinstance(message, dict):
        return 200, _error(None, INVALID_REQUEST, "Batch requests are not supported")
    if message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
        return 200, _error(message.get("id"), INVALID_REQUEST, "Invalid Request")

    method = message["method"]
    params = message.get("params") or {}
    is_notification = "id" not in message
    id_ = message.get("id")
    if not isinstance(params, dict):
        return 200, _error(id_, INVALID_PARAMS, "params must be an object")

    if method.startswith("notifications/"):
        return 202, None
    if method == "ping":
        result: Any = {}
    elif method == "initialize":
        result = _initialize(params, ctx)
    elif method == "tools/list":
        result = _tools_list(params, ctx)
    elif method == "tools/call":
        result = _tools_call(params, ctx)
        if result is None:
            return 200, _error(id_, INVALID_PARAMS, "Unknown tool")
    else:
        return 200, _error(id_, METHOD_NOT_FOUND, "Method not found")

    if is_notification:
        return 202, None
    return 200, _result(id_, result)
