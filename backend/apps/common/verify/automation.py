"""automation module — the n8n admin panel must be GATED for outsiders.

/admin/ sits behind Traefik basicAuth in prod; an anonymous 200 with panel
content means the gate fell off — that is the failure this check exists for.
"""
from __future__ import annotations

import requests

from apps.common.verify.base import TIMEOUT, ModuleReport, VerifyContext, failed, passed

PATH = "/admin/automation/"


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    # A FRESH request: the main session may hold an authenticated cookie.
    resp = requests.get(ctx.target + PATH, timeout=TIMEOUT, allow_redirects=True)
    gated = (
        resp.status_code in (401, 403)
        or (resp.status_code == 200 and "login" in resp.url)
    )
    if gated:
        results.append(passed("admin automation panel is gated", f"HTTP {resp.status_code}"))
    else:
        results.append(failed("admin automation panel is gated",
                              f"anonymous HTTP {resp.status_code} at {resp.url}"))
    return ModuleReport("automation", results)
