"""Tool registry for the MCP endpoint (spec §5/§6).

Handlers are ``(ctx, arguments) -> dict`` and may raise :class:`ToolError`.
Argument validation is done by each handler with a DRF serializer; the
``input_schema`` here is what ``tools/list`` advertises.
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from typing import Any, Callable

from rest_framework import serializers

from apps.common.errors import _stringify

Handler = Callable[["ToolContext", dict[str, Any]], dict[str, Any]]


class ToolError(Exception):
    def __init__(self, code: str, detail: str, extra: dict | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.extra = extra


@dataclass(frozen=True)
class ToolContext:
    token: Any
    membership: Any
    org: Any
    user: Any
    ip: str | None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    scope: str
    input_schema: dict[str, Any]
    handler: Handler

    def descriptor(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description,
                "inputSchema": self.input_schema}


REGISTRY: dict[str, Tool] = {}


def register(name: str, *, description: str, scope: str, input_schema: dict[str, Any]):
    def decorator(fn: Handler) -> Handler:
        REGISTRY[name] = Tool(name, description, scope, input_schema, fn)
        return fn
    return decorator


def validate(serializer_class: type[serializers.Serializer], arguments: dict[str, Any]) -> dict[str, Any]:
    """Run a DRF serializer over tool arguments; map failure to ``invalid_arguments``."""
    serializer = serializer_class(data=arguments)
    if not serializer.is_valid():
        # Errors may nest (ListField children keyed by index, nested
        # serializers as dicts); walk down to the first human-readable string.
        field, errors = next(iter(serializer.errors.items()))
        raise ToolError("invalid_arguments", f"{field}: {_stringify(errors)}")
    return serializer.validated_data


def tool_ok(data: dict[str, Any], text: str | None = None) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": text or json.dumps(data, default=str)}],
        "structuredContent": data,
        "isError": False,
    }


def tool_error(code: str, detail: str, /, **extra: Any) -> dict[str, Any]:
    # Positional-only + extras first: an extra named ``code``/``detail`` can
    # never replace the two required keys.
    payload = {"error": {**extra, "code": code, "detail": detail}}
    return {
        "content": [{"type": "text", "text": f"{code}: {detail}"}],
        "structuredContent": payload,
        "isError": True,
    }


def load_all() -> None:
    """Import the handler modules so their ``@register`` calls run.

    Tolerates a handler module that does not exist yet (mirrors
    ``_include_if_present`` in ``config/urls.py``), but a module that exists
    and fails to import still raises.
    """
    for name in ("tools_jobs", "tools_applications"):
        try:
            importlib.import_module(f"apps.tenancy.mcp.{name}")
        except ModuleNotFoundError as exc:
            if exc.name != f"apps.tenancy.mcp.{name}":
                raise
