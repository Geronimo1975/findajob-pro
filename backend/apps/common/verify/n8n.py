"""n8n module — findajob workflows on the personal n8n instance are healthy."""
from __future__ import annotations

import requests

from apps.common.verify.base import TIMEOUT, ModuleReport, VerifyContext, failed, passed, skipped

TAG = "findajob"


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    url = ctx.env.get("N8N_VERIFY_URL", "").rstrip("/")
    key = ctx.env.get("N8N_VERIFY_API_KEY")
    if not url or not key:
        results.append(skipped("n8n workflows", "set N8N_VERIFY_URL + N8N_VERIFY_API_KEY to enable"))
        return ModuleReport("n8n", results)
    headers = {"X-N8N-API-KEY": key}
    resp = requests.get(f"{url}/api/v1/workflows", params={"tags": TAG},
                        headers=headers, timeout=TIMEOUT)
    if resp.status_code != 200:
        results.append(failed("list findajob workflows", f"HTTP {resp.status_code}"))
        return ModuleReport("n8n", results)
    workflows = resp.json().get("data", [])
    if not workflows:
        results.append(skipped("n8n workflows", f"no workflows tagged '{TAG}'"))
        return ModuleReport("n8n", results)
    for wf in workflows:
        name = wf.get("name", wf.get("id"))
        results.append(passed(f"workflow '{name}' active") if wf.get("active")
                       else failed(f"workflow '{name}' active", "inactive"))
        ex = requests.get(f"{url}/api/v1/executions",
                          params={"workflowId": wf["id"], "limit": 1},
                          headers=headers, timeout=TIMEOUT)
        latest = (ex.json().get("data") or [None])[0] if ex.status_code == 200 else None
        if latest is None:
            results.append(skipped(f"workflow '{name}' last execution", "no executions yet"))
        else:
            status = latest.get("status")
            if status == "success":
                results.append(passed(f"workflow '{name}' last execution"))
            elif status in ("error", "crashed"):
                results.append(failed(f"workflow '{name}' last execution", f"status={status}"))
            else:
                results.append(skipped(f"workflow '{name}' last execution", f"status={status}"))
    return ModuleReport("n8n", results)
