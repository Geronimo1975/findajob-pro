"""mcp module — the /mcp/ endpoint answers with the envelope, and (with
MCP_VERIFY_TOKEN) advertises the phase-1 tool set. Never FAILs for a missing
credential or an undeployed route."""
from __future__ import annotations

from apps.common.verify.base import TIMEOUT, ModuleReport, VerifyContext, failed, passed, skipped

EXPECTED_TOOLS = {
    "list_jobs", "get_job", "list_applications", "get_application",
    "draft_application", "update_application", "mark_sent_manually", "send_application",
}
_PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}


def _url(ctx: VerifyContext) -> str:
    return f"{ctx.target}/mcp/"


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    resp = ctx.session.post(_url(ctx), json=_PING, timeout=TIMEOUT)
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    code = ((payload or {}).get("error") or {}).get("code")
    if resp.status_code == 404:
        results.append(skipped("mcp endpoint", "not deployed on this target (MCP_SERVER_ENABLED off or no nginx route)"))
        return ModuleReport("mcp", results)
    if resp.status_code == 401 and code == "token_missing":
        results.append(passed("mcp endpoint rejects anonymous with envelope"))
    else:
        results.append(failed("mcp endpoint", f"expected 401 token_missing, got HTTP {resp.status_code} {code}"))
        return ModuleReport("mcp", results)

    token = ctx.env.get("MCP_VERIFY_TOKEN")
    if not token:
        results.append(skipped("tools/list", "set MCP_VERIFY_TOKEN to probe with a real token"))
        return ModuleReport("mcp", results)
    resp = ctx.session.post(_url(ctx), json=_LIST, timeout=TIMEOUT,
                            headers={"Authorization": f"Bearer {token}"})
    try:
        names = {t["name"] for t in resp.json()["result"]["tools"]}
    except (ValueError, KeyError, TypeError):
        results.append(failed("tools/list", f"HTTP {resp.status_code}: {resp.text[:120]}"))
        return ModuleReport("mcp", results)
    missing = EXPECTED_TOOLS - names
    results.append(failed("tools/list", f"missing tools: {sorted(missing)}") if missing
                   else passed("tools/list advertises the phase-1 tool set"))
    return ModuleReport("mcp", results)
