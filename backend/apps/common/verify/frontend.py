"""frontend module — the public shell must serve, self-hosted only (hard rule)."""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from apps.common.verify.base import (
    TIMEOUT, ModuleReport, VerifyContext, failed, passed,
)

PAGES = ["/", "/app.html", "/auth.html"]
SUPPORT_FILES = ["/api.js", "/legal.js"]
_SRC_HREF = re.compile(r"""(?:src|href)\s*=\s*["']([^"']+)["']""", re.I)
_RESOURCES = re.compile(r"window\.__resources\s*=\s*(\{[^;]*\})", re.S)


def third_party_origins(html: str, target: str) -> set[str]:
    own = urlparse(target).netloc
    found = set()
    for url in _SRC_HREF.findall(html):
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc != own:
            found.add(f"{parsed.scheme}://{parsed.netloc}")
    return found


def resource_urls(html: str) -> list[str]:
    m = _RESOURCES.search(html)
    if not m:
        return []
    try:
        return list(json.loads(m.group(1)).values())
    except json.JSONDecodeError:
        return []


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    for page in PAGES + SUPPORT_FILES:
        try:
            resp = ctx.session.get(ctx.target + page, timeout=TIMEOUT)
        except Exception as exc:
            results.append(failed(f"GET {page}", repr(exc)))
            continue
        if resp.status_code != 200 or not resp.text.strip():
            results.append(failed(f"GET {page}", f"HTTP {resp.status_code}"))
            continue
        results.append(passed(f"GET {page}"))
        if page in PAGES:
            csp = resp.headers.get("Content-Security-Policy", "")
            if "default-src 'self'" in csp:
                results.append(passed(f"{page} CSP default-src 'self'"))
            else:
                results.append(failed(f"{page} CSP default-src 'self'", csp or "no CSP header"))
            offenders = third_party_origins(resp.text, ctx.target)
            if offenders:
                results.append(failed(f"{page} third-party origins", ", ".join(sorted(offenders))))
            else:
                results.append(passed(f"{page} no third-party origins"))
            for url in resource_urls(resp.text):
                full = url if url.startswith("http") else urljoin(ctx.target + "/", url)
                try:
                    r = ctx.session.get(full, timeout=TIMEOUT)
                    ok = r.status_code == 200
                except Exception:
                    ok = False
                results.append(passed(f"resource {url}") if ok else failed(f"resource {url}"))
    return ModuleReport("frontend", results)
