from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from apps.common.verify import MODULES
from apps.common.verify import mcp


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def _ctx(env=None, responses=()):
    session = mock.Mock()
    session.post = mock.Mock(side_effect=list(responses))
    ctx = mock.Mock()
    ctx.session = session
    ctx.env = env or {}
    ctx.target = "https://findajob.pro"
    return ctx


class VerifyMcpTests(SimpleTestCase):
    def test_registered_before_logout(self):
        names = list(MODULES)
        self.assertIn("mcp", names)
        self.assertLess(names.index("mcp"), names.index("logout"))

    def test_unauthenticated_probe_expects_401_envelope(self):
        ctx = _ctx(responses=[_Resp(401, {"error": {"code": "token_missing", "detail": "x"}})])
        report = mcp.run(ctx)
        self.assertFalse(report.failed)
        self.assertEqual(report.results[0].status, "PASS")

    def test_endpoint_absent_is_skip(self):
        ctx = _ctx(responses=[_Resp(404, {"error": {"code": "not_found", "detail": "x"}})])
        report = mcp.run(ctx)
        self.assertEqual(report.results[0].status, "SKIP")

    def test_with_token_asserts_tool_names(self):
        ctx = _ctx(env={"MCP_VERIFY_TOKEN": "fjp_" + "a" * 40}, responses=[
            _Resp(401, {"error": {"code": "token_missing", "detail": "x"}}),
            _Resp(200, {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": n} for n in mcp.EXPECTED_TOOLS]}}),
        ])
        report = mcp.run(ctx)
        self.assertFalse(report.failed)
