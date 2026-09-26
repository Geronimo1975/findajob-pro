"""JSON-RPC envelope on ``/mcp/`` (spec §3)."""
from __future__ import annotations

import json
from unittest import skip

from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework import serializers

from apps.tenancy.mcp.tools import REGISTRY, ToolError, register, tool_ok, validate
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user
from tests.tenancy.mcp.support import MCP_URL, make_token, rpc


class RpcEnvelopeTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.raw, self.token = make_token(self.user, self.org)

    def test_initialize(self):
        r = rpc(self.client, self.raw, "initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}})
        self.assertEqual(r.status_code, 200)
        result = r.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "findajob.pro")
        self.assertIn("tools", result["capabilities"])
        self.assertNotIn("resources", result["capabilities"])

    def test_initialized_notification_is_202_empty(self):
        body = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
        r = rpc(self.client, self.raw, None, raw_body=body)
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.content, b"")

    def test_unknown_method(self):
        r = rpc(self.client, self.raw, "resources/list")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"]["code"], -32601)

    def test_batch_rejected(self):
        r = rpc(self.client, self.raw, None, raw_body="[]")
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_parse_error(self):
        r = rpc(self.client, self.raw, None, raw_body="{not json")
        self.assertEqual(r.json()["error"]["code"], -32700)

    def test_missing_jsonrpc_version(self):
        r = rpc(self.client, self.raw, None, raw_body=json.dumps({"id": 1, "method": "ping"}))
        self.assertEqual(r.json()["error"]["code"], -32600)

    def test_body_over_256kb_is_413_envelope(self):
        r = rpc(self.client, self.raw, None, raw_body="x" * (256 * 1024 + 1))
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["error"]["code"], "payload_too_large")

    def test_tools_list_shape(self):
        r = rpc(self.client, self.raw, "tools/list")
        tools = r.json()["result"]["tools"]
        self.assertIsInstance(tools, list)
        for tool in tools:
            self.assertEqual(set(tool) >= {"name", "description", "inputSchema"}, True)

    def test_tools_call_unknown_tool(self):
        r = rpc(self.client, self.raw, "tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(r.json()["error"]["code"], -32602)

    @skip("Task 5")
    def test_tools_call_missing_scope(self):
        raw, _ = make_token(self.user, self.org, scopes=["applications:read"])
        r = rpc(self.client, raw, "tools/call", {"name": "list_jobs", "arguments": {}})
        result = r.json()["result"]
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error"]["code"], "scope_missing")

    def test_non_object_params_is_invalid_params_not_500(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": ["x"]})
        r = rpc(self.client, self.raw, None, raw_body=body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"]["code"], -32602)

    def test_non_string_tool_name_is_invalid_params_not_500(self):
        r = rpc(self.client, self.raw, "tools/call", {"name": ["x"], "arguments": {}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"]["code"], -32602)

    def test_deeply_nested_json_is_parse_error_not_500(self):
        body = "[" * 100000 + "]" * 100000
        r = rpc(self.client, self.raw, None, raw_body=body)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["error"]["code"], -32700)


class ToolContextIpTests(TestCase):
    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_ctx_ip_honours_trusted_proxy_count(self):
        user = make_user()
        org = bootstrap_workspace(user).org
        raw, _ = make_token(user, org)
        seen = {}

        @register("_test_echo_ip", description="test", scope="jobs:read",
                  input_schema={"type": "object"})
        def _echo(ctx, arguments):
            seen["ip"] = ctx.ip
            return tool_ok({})

        try:
            self.client.post(
                MCP_URL,
                data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                 "params": {"name": "_test_echo_ip", "arguments": {}}}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {raw}",
                HTTP_X_FORWARDED_FOR="203.0.113.9", REMOTE_ADDR="10.0.0.2",
            )
        finally:
            REGISTRY.pop("_test_echo_ip", None)
        self.assertEqual(seen["ip"], "203.0.113.9")


class ValidateTests(SimpleTestCase):
    def test_nested_list_child_error_is_invalid_arguments(self):
        class Args(serializers.Serializer):
            ids = serializers.ListField(child=serializers.IntegerField())

        with self.assertRaises(ToolError) as caught:
            validate(Args, {"ids": ["x"]})
        self.assertEqual(caught.exception.code, "invalid_arguments")
        self.assertTrue(caught.exception.detail.startswith("ids: "))
        self.assertIn("integer", caught.exception.detail.lower())

    def test_nested_dict_error_is_invalid_arguments(self):
        class Inner(serializers.Serializer):
            n = serializers.IntegerField()

        class Args(serializers.Serializer):
            inner = Inner()

        with self.assertRaises(ToolError) as caught:
            validate(Args, {"inner": {"n": "x"}})
        self.assertEqual(caught.exception.code, "invalid_arguments")
        self.assertTrue(caught.exception.detail.startswith("inner: "))
