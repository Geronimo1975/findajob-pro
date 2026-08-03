"""api-gate module — unauthenticated behaviour of the API front door."""
from __future__ import annotations

from apps.common.verify.base import TIMEOUT, ModuleReport, VerifyContext, failed, passed


def is_error_shape(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    return (
        isinstance(err, dict)
        and isinstance(err.get("code"), str)
        and isinstance(err.get("detail"), str)
    )


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    # /me/ unauthenticated → 401 canonical shape
    resp = ctx.session.get(ctx.api("/me/"), timeout=TIMEOUT)
    if resp.status_code == 401 and is_error_shape(_json(resp)):
        results.append(passed("/me/ unauthenticated → 401 canonical"))
    else:
        results.append(failed("/me/ unauthenticated → 401 canonical",
                              f"HTTP {resp.status_code}: {resp.text[:120]}"))
    # csrf cookie
    resp = ctx.session.get(ctx.api("/auth/csrf/"), timeout=TIMEOUT)
    if resp.status_code == 200 and any("csrf" in c.lower() for c in ctx.session.cookies.keys()):
        results.append(passed("/auth/csrf/ sets CSRF cookie"))
    else:
        results.append(failed("/auth/csrf/ sets CSRF cookie", f"HTTP {resp.status_code}"))
    # unknown path under /api/v1/ keeps the envelope
    resp = ctx.session.get(ctx.api("/definitely-not-a-route/"), timeout=TIMEOUT)
    if resp.status_code == 404 and is_error_shape(_json(resp)):
        results.append(passed("/api/v1/ 404 keeps canonical shape"))
    else:
        results.append(failed("/api/v1/ 404 keeps canonical shape", f"HTTP {resp.status_code}"))
    return ModuleReport("api-gate", results)
