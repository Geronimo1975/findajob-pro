"""Shared helpers for the ``/mcp/`` endpoint tests."""
from __future__ import annotations

import json

from apps.tenancy.mcp.models import SCOPES, McpToken, generate_token

MCP_URL = "/mcp/"


def make_token(user, org, scopes=SCOPES, **overrides) -> tuple[str, McpToken]:
    raw, prefix, digest = generate_token()
    fields = dict(org=org, user=user, name="test", prefix=prefix,
                  token_hash=digest, scopes=list(scopes))
    fields.update(overrides)
    return raw, McpToken.objects.create(**fields)


def rpc(client, raw_token, method, params=None, id=1, *, raw_body=None):
    body = raw_body if raw_body is not None else json.dumps(
        {"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}}
    )
    headers = {"HTTP_AUTHORIZATION": f"Bearer {raw_token}"} if raw_token else {}
    return client.post(MCP_URL, data=body, content_type="application/json", **headers)


def call_tool(client, raw_token, name, arguments=None, id=1):
    return rpc(client, raw_token, "tools/call",
               {"name": name, "arguments": arguments or {}}, id=id)
