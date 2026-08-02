# verify_platform Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `manage.py verify_platform` command that smoke-tests every module of findajob.pro from the outside as a real visitor — frontend, API gate, full OTP login, mail deliverability (Inbox-not-Spam + SPF/DKIM/DMARC), outbox, automation panel gating, and n8n — with PASS/FAIL/SKIP per check and non-zero exit on any FAIL.

**Architecture:** A `apps/common/verify/` package: one small module per verified area, each exposing `run(ctx) -> ModuleReport`; a shared `VerifyContext` carries the target URL, one `requests.Session` (cookies persist across modules), and cross-module state (the fetched OTP mail). The management command is a thin runner/printer. Gmail is read over IMAP with an app password; DNS via `dig` subprocess. It complements the existing `check_login_flow` command — that one proves the SMTP leg without mailing anyone; this one proves *real end-to-end delivery* to a real inbox.

**Tech Stack:** Django 6 management command, `requests` (already pinned), stdlib `imaplib`/`email`/`subprocess`, pytest with `unittest.mock` (no live network in tests).

## Global Constraints

- All commands run from `backend/`; tests via `../.venv/bin/pytest`.
- No new dependencies (spec: "No new dependencies at all").
- Every API error must match the canonical shape `{"error": {"code": ..., "detail": ...}}`.
- SKIP never fails the run; only FAIL sets exit code 1. Missing env credentials → SKIP with a "how to enable" hint; network timeouts against the target → FAIL.
- Per-request timeout 15s; OTP mail wait up to 90s.
- Nothing in this command mails a third party: the only mail triggered goes to the operator-owned `VERIFY_TEST_EMAIL` (default `george@rotocon.world`).
- Env vars: `VERIFY_TEST_EMAIL`, `VERIFY_GMAIL_USER`, `VERIFY_GMAIL_APP_PASSWORD`, `N8N_VERIFY_URL`, `N8N_VERIFY_API_KEY`.
- Target endpoints (from `config/urls.py`): `/api/v1/auth/{csrf,request-code,verify-code,logout}/`, `/api/v1/me/`, `/api/v1/outbox/mailbox/` (+`test/`), `/admin/automation/` (admin-gated panel).
- OTP mail subject (EN): `"{code} is your findajob.pro login code"` — the code is the leading 6 digits of the subject.

---

### Task 1: Verify framework — results, context, runner, command skeleton

**Files:**
- Create: `apps/common/verify/__init__.py`
- Create: `apps/common/verify/base.py`
- Create: `apps/common/management/commands/verify_platform.py`
- Test: `tests/common/test_verify_platform.py`

**Interfaces:**
- Produces: `CheckResult(name, status, detail="")` with `status in {"PASS","FAIL","SKIP"}`; `ModuleReport(module, results)` with `.failed` property; `VerifyContext(target, session, env)` with `.api(path)->str` (joins `target + "/api/v1" + path`), `.otp_message` (`email.message.Message | None`), `.otp_requested_at` (`datetime | None`); `passed(name, detail="")`, `failed(name, detail="")`, `skipped(name, detail="")` helpers returning `CheckResult`; `MODULES: dict[str, Callable[[VerifyContext], ModuleReport]]` registry in `apps/common/verify/__init__.py` (filled by later tasks, ordered: frontend, api-gate, auth-otp, mail-deliverability, outbox, automation, n8n).

- [ ] **Step 1: Write the failing tests**

```python
# tests/common/test_verify_platform.py
"""Unit tests for the verify_platform framework. No live network."""
from __future__ import annotations

import json
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command

from apps.common.verify.base import (
    CheckResult, ModuleReport, VerifyContext, failed, passed, skipped,
)


class TestReportAggregation:
    def test_module_report_failed_iff_any_fail(self):
        ok = ModuleReport("m", [passed("a"), skipped("b", "no creds")])
        bad = ModuleReport("m", [passed("a"), failed("c", "boom")])
        assert ok.failed is False
        assert bad.failed is True

    def test_skip_never_fails(self):
        rep = ModuleReport("m", [skipped("a"), skipped("b")])
        assert rep.failed is False


class TestCommand:
    def _run(self, modules, *args):
        out = StringIO()
        with mock.patch("apps.common.management.commands.verify_platform.MODULES", modules):
            call_command("verify_platform", *args, stdout=out)
        return out.getvalue()

    def test_exit_zero_when_all_pass(self):
        modules = {"m1": lambda ctx: ModuleReport("m1", [passed("ok")])}
        out = self._run(modules)
        assert "PASS" in out and "m1" in out

    def test_exit_one_on_any_fail(self):
        modules = {"m1": lambda ctx: ModuleReport("m1", [failed("x", "boom")])}
        with pytest.raises(SystemExit) as exc:
            self._run(modules)
        assert exc.value.code == 1

    def test_module_filter(self):
        seen = []
        modules = {
            "m1": lambda ctx: (seen.append("m1"), ModuleReport("m1", [passed("a")]))[1],
            "m2": lambda ctx: (seen.append("m2"), ModuleReport("m2", [passed("a")]))[1],
        }
        self._run(modules, "--module", "m2")
        assert seen == ["m2"]

    def test_json_output(self):
        modules = {"m1": lambda ctx: ModuleReport("m1", [skipped("a", "hint")])}
        out = self._run(modules, "--json")
        data = json.loads(out)
        assert data["modules"][0] == {
            "module": "m1",
            "results": [{"name": "a", "status": "SKIP", "detail": "hint"}],
        }

    def test_module_exception_becomes_fail(self):
        def blow_up(ctx):
            raise RuntimeError("connection refused")
        modules = {"m1": blow_up}
        with pytest.raises(SystemExit):
            out = StringIO()
            with mock.patch(
                "apps.common.management.commands.verify_platform.MODULES", modules
            ):
                call_command("verify_platform", stdout=out)


class TestContext:
    def test_api_joins(self):
        ctx = VerifyContext(target="https://findajob.pro", session=mock.Mock(), env={})
        assert ctx.api("/me/") == "https://findajob.pro/api/v1/me/"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && ../.venv/bin/pytest tests/common/test_verify_platform.py -v`
Expected: FAIL — `ModuleNotFoundError: apps.common.verify`

- [ ] **Step 3: Implement**

```python
# apps/common/verify/base.py
"""Shared types for verify_platform modules (spec: docs/superpowers/specs/2026-08-02-verify-platform-design.md)."""
from __future__ import annotations

import email.message
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

import requests

TIMEOUT = 15  #: per-request timeout, seconds (spec "Degradation rules")
OTP_WAIT = 90  #: max seconds to wait for the OTP mail


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""


def passed(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name, "PASS", detail)


def failed(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name, "FAIL", detail)


def skipped(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name, "SKIP", detail)


@dataclass
class ModuleReport:
    module: str
    results: list[CheckResult]

    @property
    def failed(self) -> bool:
        return any(r.status == "FAIL" for r in self.results)


@dataclass
class VerifyContext:
    """State shared across modules: one cookie-carrying session, cross-module mail."""

    target: str
    session: requests.Session
    env: Mapping[str, str]
    otp_message: email.message.Message | None = None
    otp_folder: str | None = None  # "INBOX" or "[Gmail]/Spam"
    otp_requested_at: datetime | None = None

    def api(self, path: str) -> str:
        return f"{self.target}/api/v1{path}"
```

```python
# apps/common/verify/__init__.py
"""Module registry — insertion order is execution order."""
from __future__ import annotations

from typing import Callable

from apps.common.verify.base import ModuleReport, VerifyContext

MODULES: dict[str, Callable[[VerifyContext], ModuleReport]] = {}
# Later tasks register: frontend, api-gate, auth-otp, mail-deliverability,
# outbox, automation, n8n — by importing their module and adding it here.
```

```python
# apps/common/management/commands/verify_platform.py
"""``manage.py verify_platform`` — external smoke verification of findajob.pro.

Complements ``check_login_flow``: that command proves the SMTP leg without
mailing anyone; this one proves real end-to-end delivery to a real inbox and
sweeps every user-facing module. Spec:
docs/superpowers/specs/2026-08-02-verify-platform-design.md
"""
from __future__ import annotations

import json
import os
import sys

import requests
from django.core.management.base import BaseCommand, CommandError

from apps.common.verify import MODULES
from apps.common.verify.base import ModuleReport, VerifyContext, failed


class Command(BaseCommand):
    help = "Smoke-verify the live platform from the outside, module by module."

    def add_arguments(self, parser):
        parser.add_argument("--target", default="https://findajob.pro")
        parser.add_argument("--module", action="append", dest="modules")
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **opts):
        selected = opts["modules"] or list(MODULES)
        unknown = [m for m in selected if m not in MODULES]
        if unknown:
            raise CommandError(f"unknown module(s): {', '.join(unknown)}")

        ctx = VerifyContext(
            target=opts["target"].rstrip("/"),
            session=requests.Session(),
            env=os.environ,
        )
        reports: list[ModuleReport] = []
        for name in selected:
            try:
                reports.append(MODULES[name](ctx))
            except Exception as exc:  # a crashed module is a FAIL, not a traceback
                reports.append(ModuleReport(name, [failed("module crashed", repr(exc))]))

        if opts["as_json"]:
            self.stdout.write(json.dumps({
                "target": ctx.target,
                "modules": [
                    {"module": r.module,
                     "results": [vars(c) for c in r.results]}
                    for r in reports
                ],
            }, indent=2))
        else:
            for r in reports:
                self.stdout.write(f"\n== {r.module} ==")
                for c in r.results:
                    line = f"  [{c.status}] {c.name}"
                    if c.detail:
                        line += f" — {c.detail}"
                    self.stdout.write(line)

        if any(r.failed for r in reports):
            sys.exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && ../.venv/bin/pytest tests/common/test_verify_platform.py -v`
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add apps/common/verify backend/apps/common/management/commands/verify_platform.py tests/common/test_verify_platform.py
git commit -m "feat(verify): verify_platform framework, runner and report types"
```

---

### Task 2: `frontend` module

**Files:**
- Create: `apps/common/verify/frontend.py`
- Modify: `apps/common/verify/__init__.py` (register)
- Test: `tests/common/test_verify_platform.py` (append)

**Interfaces:**
- Consumes: `VerifyContext`, result helpers from Task 1.
- Produces: `run(ctx) -> ModuleReport` registered as `MODULES["frontend"]`; pure helpers `third_party_origins(html: str, target: str) -> set[str]` and `resource_urls(html: str) -> list[str]` (parses the `window.__resources = {...}` JSON object literal).

- [ ] **Step 1: Write the failing tests** (append to the test file)

```python
from apps.common.verify.frontend import resource_urls, third_party_origins


class TestFrontendHelpers:
    def test_third_party_origin_detected(self):
        html = '<script src="https://cdn.example.com/x.js"></script>'
        assert third_party_origins(html, "https://findajob.pro") == {"https://cdn.example.com"}

    def test_same_origin_and_relative_ok(self):
        html = ('<script src="/vendor/react.js"></script>'
                '<link href="https://findajob.pro/fonts/a.woff2">')
        assert third_party_origins(html, "https://findajob.pro") == set()

    def test_resource_urls_parsed(self):
        html = ('<script>window.__resources = {"react": "/vendor/react.production.js",'
                ' "font": "/fonts/inter.woff2"};</script>')
        assert resource_urls(html) == ["/vendor/react.production.js", "/fonts/inter.woff2"]

    def test_resource_urls_absent(self):
        assert resource_urls("<html></html>") == []
```

- [ ] **Step 2: Run tests → FAIL** (`ImportError`)

- [ ] **Step 3: Implement**

```python
# apps/common/verify/frontend.py
"""frontend module — the public shell must serve, self-hosted only (hard rule)."""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from apps.common.verify.base import (
    TIMEOUT, ModuleReport, VerifyContext, failed, passed, skipped,
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
                full = url if url.startswith("http") else ctx.target + url
                try:
                    r = ctx.session.get(full, timeout=TIMEOUT)
                    ok = r.status_code == 200
                except Exception:
                    ok = False
                results.append(passed(f"resource {url}") if ok else failed(f"resource {url}"))
    return ModuleReport("frontend", results)
```

Register in `apps/common/verify/__init__.py`:

```python
from apps.common.verify import frontend

MODULES["frontend"] = frontend.run
```

- [ ] **Step 4: Run the whole test file → PASS**
- [ ] **Step 5: Commit** — `feat(verify): frontend module (pages, CSP, self-hosted assets)`

---

### Task 3: `api-gate` module

**Files:**
- Create: `apps/common/verify/api_gate.py`
- Modify: `apps/common/verify/__init__.py` (register after frontend)
- Test: `tests/common/test_verify_platform.py` (append)

**Interfaces:**
- Consumes: `VerifyContext`.
- Produces: `MODULES["api-gate"]`; helper `is_error_shape(payload) -> bool` (True iff `payload == {"error": {"code": str, "detail": str}}`-shaped) — **reused by outbox/automation tasks**.

- [ ] **Step 1: Write the failing tests**

```python
from apps.common.verify.api_gate import is_error_shape


class TestErrorShape:
    def test_canonical_shape(self):
        assert is_error_shape({"error": {"code": "not_authenticated", "detail": "x"}})

    def test_rejects_drf_default(self):
        assert not is_error_shape({"detail": "Not found."})
        assert not is_error_shape({"error": "nope"})
        assert not is_error_shape(None)
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

```python
# apps/common/verify/api_gate.py
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
```

Register: `MODULES["api-gate"] = api_gate.run`.

- [ ] **Step 4: Run tests → PASS**
- [ ] **Step 5: Commit** — `feat(verify): api-gate module (401 shape, csrf cookie, 404 envelope)`

---

### Task 4: Gmail IMAP helper

**Files:**
- Create: `apps/common/verify/gmail.py`
- Test: `tests/common/test_verify_platform.py` (append)

**Interfaces:**
- Produces: `GmailClient(user, app_password)` with `wait_for_message(*, subject_contains: str, since: datetime, timeout: int = 90, poll: float = 5.0) -> FoundMessage | None` (searches `INBOX` then `[Gmail]/Spam`, matching `SINCE` + subject substring, newest first); `@dataclass FoundMessage(folder: str, message: email.message.Message)`; `extract_otp(subject: str) -> str | None` (leading 6 digits); `auth_results(message) -> dict[str, str]` (lowercased `{"spf": "pass", "dkim": "pass", "dmarc": "pass"}` parsed from all `Authentication-Results` headers).

- [ ] **Step 1: Write the failing tests**

```python
import email as email_lib

from apps.common.verify.gmail import auth_results, extract_otp


class TestOtpExtraction:
    def test_extracts_leading_six_digits(self):
        assert extract_otp("482913 is your findajob.pro login code") == "482913"

    def test_german_subject(self):
        assert extract_otp("482913 ist Ihr Anmeldecode für findajob.pro") == "482913"

    def test_no_code(self):
        assert extract_otp("Welcome to findajob.pro") is None


class TestAuthResults:
    def test_parses_pass_verdicts(self):
        raw = (
            "Authentication-Results: mx.google.com;\n"
            " spf=pass (google.com: domain of x designates y) smtp.mailfrom=x;\n"
            " dkim=pass header.i=@mail.findajob.pro;\n"
            " dmarc=pass (p=NONE) header.from=findajob.pro\n"
            "From: findajob.pro <noreply@findajob.pro>\n"
            "Subject: t\n\nbody\n"
        )
        msg = email_lib.message_from_string(raw)
        assert auth_results(msg) == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}

    def test_missing_header(self):
        msg = email_lib.message_from_string("Subject: t\n\nbody\n")
        assert auth_results(msg) == {}
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

```python
# apps/common/verify/gmail.py
"""Read-only Gmail access over IMAP for deliverability proof.

App password only (VERIFY_GMAIL_USER / VERIFY_GMAIL_APP_PASSWORD) so the
command stays autonomous and cron-able.
"""
from __future__ import annotations

import email
import imaplib
import re
import time
from dataclasses import dataclass
from datetime import datetime

FOLDERS = ["INBOX", "[Gmail]/Spam"]
_OTP = re.compile(r"^(\d{6})\b")
_VERDICT = re.compile(r"\b(spf|dkim|dmarc)=(\w+)", re.I)


@dataclass
class FoundMessage:
    folder: str
    message: email.message.Message


def extract_otp(subject: str) -> str | None:
    m = _OTP.match(subject.strip())
    return m.group(1) if m else None


def auth_results(message: email.message.Message) -> dict[str, str]:
    verdicts: dict[str, str] = {}
    for header in message.get_all("Authentication-Results") or []:
        for mech, verdict in _VERDICT.findall(header):
            verdicts.setdefault(mech.lower(), verdict.lower())
    return verdicts


class GmailClient:
    def __init__(self, user: str, app_password: str):
        self.user = user
        self.app_password = app_password

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL("imap.gmail.com")
        conn.login(self.user, self.app_password)
        return conn

    def _search(self, conn, folder: str, subject_contains: str, since: datetime):
        # SINCE is date-granular; the caller's subject filter plus newest-first
        # ordering keeps stale same-day mails from matching before fresh ones.
        if conn.select(f'"{folder}"', readonly=True)[0] != "OK":
            return None
        date = since.strftime("%d-%b-%Y")
        status, data = conn.search(None, "SINCE", date, "SUBJECT", f'"{subject_contains}"')
        if status != "OK" or not data or not data[0]:
            return None
        latest = data[0].split()[-1]
        status, msg_data = conn.fetch(latest, "(RFC822)")
        if status != "OK":
            return None
        return email.message_from_bytes(msg_data[0][1])

    def wait_for_message(self, *, subject_contains: str, since: datetime,
                         timeout: int = 90, poll: float = 5.0) -> FoundMessage | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            conn = self._connect()
            try:
                for folder in FOLDERS:
                    msg = self._search(conn, folder, subject_contains, since)
                    if msg is not None:
                        return FoundMessage(folder=folder, message=msg)
            finally:
                conn.logout()
            time.sleep(poll)
        return None
```

- [ ] **Step 4: Run tests → PASS**
- [ ] **Step 5: Commit** — `feat(verify): read-only Gmail IMAP helper (OTP + Authentication-Results)`

---

### Task 5: `auth-otp` module — full real login

**Files:**
- Create: `apps/common/verify/auth_otp.py`
- Modify: `apps/common/verify/__init__.py` (register)
- Test: `tests/common/test_verify_platform.py` (append)

**Interfaces:**
- Consumes: `GmailClient`, `extract_otp` (Task 4); `is_error_shape` (Task 3).
- Produces: `MODULES["auth-otp"]`. Side effects on ctx: sets `ctx.otp_message`, `ctx.otp_folder`, `ctx.otp_requested_at`; on success the session cookie jar holds a **live authenticated session** (logout is deferred to a final `_logout` check *after* outbox/automation run — implemented here as `finish(ctx)`, registered as the **last** module `"logout"`).
- CSRF: every POST sends header `X-CSRFToken: <csrf cookie value>` and `Referer: ctx.target` (Django's HTTPS origin check requires it).

- [ ] **Step 1: Write the failing tests**

```python
from apps.common.verify.auth_otp import csrf_headers


class TestCsrfHeaders:
    def test_reads_token_from_cookiejar(self):
        session = mock.Mock()
        session.cookies.get.return_value = "tok123"
        h = csrf_headers(session, "https://findajob.pro")
        assert h["X-CSRFToken"] == "tok123"
        assert h["Referer"] == "https://findajob.pro/"


class TestAuthOtpSkips:
    def test_skips_without_gmail_creds(self):
        """request-code 202 still runs; code fetch SKIPs, nothing FAILs."""
        session = mock.Mock()
        resp202 = mock.Mock(status_code=202)
        respcsrf = mock.Mock(status_code=200)
        session.get.return_value = respcsrf
        session.post.return_value = resp202
        session.cookies.get.return_value = "tok"
        session.cookies.keys.return_value = ["csrftoken"]
        from apps.common.verify import auth_otp
        ctx = VerifyContext(target="https://x", session=session, env={})
        report = auth_otp.run(ctx)
        assert report.failed is False
        assert any(r.status == "SKIP" for r in report.results)
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

```python
# apps/common/verify/auth_otp.py
"""auth-otp module — a real end-to-end login for VERIFY_TEST_EMAIL.

Unlike check_login_flow (which suppresses the send and proves only the SMTP
envelope), this module lets the real mail go out to the operator-owned test
address and reads the code back over IMAP. The mail it fetches is handed to
mail-deliverability via ctx.otp_message.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from apps.common.verify.base import (
    OTP_WAIT, TIMEOUT, ModuleReport, VerifyContext, failed, passed, skipped,
)
from apps.common.verify.gmail import GmailClient, extract_otp

SUBJECT_MARKER = "findajob.pro login code"  # EN subject: "{code} is your findajob.pro login code"


def csrf_headers(session: requests.Session, target: str) -> dict[str, str]:
    token = session.cookies.get("csrftoken") or ""
    return {"X-CSRFToken": token, "Referer": target + "/"}


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    test_email = ctx.env.get("VERIFY_TEST_EMAIL", "george@rotocon.world")
    ctx.session.get(ctx.api("/auth/csrf/"), timeout=TIMEOUT)

    # SINCE is date-granular; back off a minute so clock skew can't hide the mail.
    ctx.otp_requested_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    resp = ctx.session.post(
        ctx.api("/auth/request-code/"), json={"email": test_email},
        headers=csrf_headers(ctx.session, ctx.target), timeout=TIMEOUT,
    )
    if resp.status_code == 202:
        results.append(passed("request-code → 202"))
    else:
        results.append(failed("request-code → 202", f"HTTP {resp.status_code}: {resp.text[:120]}"))
        return ModuleReport("auth-otp", results)

    user = ctx.env.get("VERIFY_GMAIL_USER")
    pw = ctx.env.get("VERIFY_GMAIL_APP_PASSWORD")
    if not user or not pw:
        results.append(skipped(
            "fetch OTP from Gmail",
            "set VERIFY_GMAIL_USER + VERIFY_GMAIL_APP_PASSWORD to enable"))
        return ModuleReport("auth-otp", results)

    found = GmailClient(user, pw).wait_for_message(
        subject_contains=SUBJECT_MARKER, since=ctx.otp_requested_at, timeout=OTP_WAIT)
    if found is None:
        results.append(failed("OTP mail arrives", f"nothing within {OTP_WAIT}s in Inbox or Spam"))
        return ModuleReport("auth-otp", results)
    ctx.otp_message, ctx.otp_folder = found.message, found.folder
    results.append(passed("OTP mail arrives", f"in {found.folder}"))

    code = extract_otp(found.message.get("Subject", ""))
    if code is None:
        results.append(failed("extract code from subject", repr(found.message.get("Subject"))))
        return ModuleReport("auth-otp", results)

    resp = ctx.session.post(
        ctx.api("/auth/verify-code/"), json={"email": test_email, "code": code},
        headers=csrf_headers(ctx.session, ctx.target), timeout=TIMEOUT,
    )
    if resp.status_code == 200:
        results.append(passed("verify-code → session"))
    else:
        results.append(failed("verify-code → session", f"HTTP {resp.status_code}: {resp.text[:120]}"))
        return ModuleReport("auth-otp", results)

    resp = ctx.session.get(ctx.api("/me/"), timeout=TIMEOUT)
    if resp.status_code == 200 and test_email in resp.text:
        results.append(passed("/me/ echoes the test account"))
    else:
        results.append(failed("/me/ echoes the test account", f"HTTP {resp.status_code}"))
    return ModuleReport("auth-otp", results)


def finish(ctx: VerifyContext) -> ModuleReport:
    """Registered last as module "logout" — runs after outbox/automation used the session."""
    results = []
    resp = ctx.session.post(ctx.api("/auth/logout/"),
                            headers=csrf_headers(ctx.session, ctx.target), timeout=TIMEOUT)
    if resp.status_code in (200, 204):
        results.append(passed("logout"))
    else:
        results.append(skipped("logout", f"HTTP {resp.status_code} (was there a session?)"))
        return ModuleReport("logout", results)
    resp = ctx.session.get(ctx.api("/me/"), timeout=TIMEOUT)
    results.append(passed("session gone (/me/ → 401)") if resp.status_code == 401
                   else failed("session gone (/me/ → 401)", f"HTTP {resp.status_code}"))
    return ModuleReport("logout", results)
```

Register: `MODULES["auth-otp"] = auth_otp.run` (after api-gate) and — at the very end of the registry, after Task 8 — `MODULES["logout"] = auth_otp.finish`.

- [ ] **Step 4: Run tests → PASS**
- [ ] **Step 5: Commit** — `feat(verify): auth-otp module (real login via Gmail-fetched code)`

---

### Task 6: `mail-deliverability` module

**Files:**
- Create: `apps/common/verify/deliverability.py`
- Modify: `apps/common/verify/__init__.py` (register after auth-otp)
- Test: `tests/common/test_verify_platform.py` (append)

**Interfaces:**
- Consumes: `ctx.otp_message` / `ctx.otp_folder` (Task 5); `auth_results` (Task 4).
- Produces: `MODULES["mail-deliverability"]`; helper `dig_txt(name: str) -> list[str]` (`dig +short TXT <name>` via subprocess, 10s timeout, strips quotes; raises `DigError` on missing binary/timeout so the caller can SKIP).

- [ ] **Step 1: Write the failing tests**

```python
from apps.common.verify.deliverability import dig_txt, run as deliverability_run


class TestDeliverability:
    def test_dig_txt_parses_and_strips_quotes(self):
        with mock.patch("subprocess.run") as sp:
            sp.return_value = mock.Mock(returncode=0, stdout='"v=spf1 include:spf.brevo.com ~all"\n')
            assert dig_txt("findajob.pro") == ["v=spf1 include:spf.brevo.com ~all"]

    def test_inbox_not_spam(self):
        raw = ("Authentication-Results: mx.google.com; spf=pass; dkim=pass; dmarc=pass\n"
               "Subject: 123456 is your findajob.pro login code\n\nbody\n")
        ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
        ctx.otp_message = email_lib.message_from_string(raw)
        ctx.otp_folder = "INBOX"
        with mock.patch("apps.common.verify.deliverability.dig_txt",
                        side_effect=lambda name: ["v=spf1 x"] if "dmarc" not in name else ["v=DMARC1; p=none"]):
            report = deliverability_run(ctx)
        by_name = {r.name: r.status for r in report.results}
        assert by_name["OTP landed in Inbox (not Spam)"] == "PASS"
        assert by_name["SPF/DKIM/DMARC all pass"] == "PASS"

    def test_spam_folder_fails(self):
        ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
        ctx.otp_message = email_lib.message_from_string("Subject: t\n\nb\n")
        ctx.otp_folder = "[Gmail]/Spam"
        with mock.patch("apps.common.verify.deliverability.dig_txt", return_value=[]):
            report = deliverability_run(ctx)
        assert report.failed is True

    def test_skips_without_otp_mail(self):
        ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
        with mock.patch("apps.common.verify.deliverability.dig_txt", return_value=[]):
            report = deliverability_run(ctx)
        assert any(r.status == "SKIP" and "auth-otp" in r.detail for r in report.results)
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

```python
# apps/common/verify/deliverability.py
"""mail-deliverability — proof the OTP mail reached a real Inbox, plus DNS posture."""
from __future__ import annotations

import subprocess

from apps.common.verify.base import ModuleReport, VerifyContext, failed, passed, skipped
from apps.common.verify.gmail import auth_results

DOMAIN = "findajob.pro"


class DigError(Exception):
    pass


def dig_txt(name: str) -> list[str]:
    try:
        proc = subprocess.run(["dig", "+short", "TXT", name],
                              capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise DigError(repr(exc)) from exc
    return [line.strip().strip('"') for line in proc.stdout.splitlines() if line.strip()]


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    if ctx.otp_message is None:
        results.append(skipped("OTP landed in Inbox (not Spam)",
                               "no OTP mail captured — see auth-otp module"))
    else:
        if ctx.otp_folder == "INBOX":
            results.append(passed("OTP landed in Inbox (not Spam)"))
        else:
            results.append(failed("OTP landed in Inbox (not Spam)", f"found in {ctx.otp_folder}"))
        verdicts = auth_results(ctx.otp_message)
        bad = {m: v for m, v in verdicts.items() if v != "pass"}
        missing = {"spf", "dkim", "dmarc"} - set(verdicts)
        if not bad and not missing:
            results.append(passed("SPF/DKIM/DMARC all pass"))
        else:
            results.append(failed("SPF/DKIM/DMARC all pass",
                                  f"failing={bad or '-'} missing={sorted(missing) or '-'}"))
    # DNS posture — independent of the mail above.
    try:
        spf = [r for r in dig_txt(DOMAIN) if r.startswith("v=spf1")]
        results.append(passed("SPF record published", spf[0]) if spf
                       else failed("SPF record published", f"no v=spf1 TXT on {DOMAIN}"))
        dmarc = [r for r in dig_txt(f"_dmarc.{DOMAIN}") if r.startswith("v=DMARC1")]
        results.append(passed("DMARC record published", dmarc[0]) if dmarc
                       else failed("DMARC record published", f"no v=DMARC1 TXT on _dmarc.{DOMAIN}"))
    except DigError as exc:
        results.append(skipped("DNS SPF/DMARC records", f"dig unavailable: {exc}"))
    return ModuleReport("mail-deliverability", results)
```

Register: `MODULES["mail-deliverability"] = deliverability.run`.

Note: the spec also names the Brevo DKIM selector. Selector names are account-specific (`mail._domainkey` vs `brevo._domainkey`); the header check (`dkim=pass`) already proves signing works end-to-end, so a selector guess would only produce false FAILs — the DNS checks stay at SPF + DMARC. This is a deliberate, documented narrowing of the spec.

- [ ] **Step 4: Run tests → PASS**
- [ ] **Step 5: Commit** — `feat(verify): mail-deliverability module (Inbox proof, auth headers, DNS posture)`

---

### Task 7: `outbox` module

**Files:**
- Create: `apps/common/verify/outbox.py`
- Modify: `apps/common/verify/__init__.py` (register)
- Test: `tests/common/test_verify_platform.py` (append)

**Interfaces:**
- Consumes: authenticated session from Task 5; `is_error_shape` (Task 3); `GmailClient` (Task 4).
- Produces: `MODULES["outbox"]`. Real endpoints: `GET /api/v1/outbox/mailbox/` (200 with mailbox, or 404 `mailbox_not_connected`), `POST /api/v1/outbox/mailbox/test/` (sends a test mail through the user's own SMTP).

- [ ] **Step 1: Write the failing tests**

```python
from apps.common.verify import outbox as outbox_mod


class TestOutbox:
    def _ctx(self, get_resp):
        session = mock.Mock()
        session.get.return_value = get_resp
        session.cookies.get.return_value = "tok"
        return VerifyContext(target="https://x", session=session, env={})

    def test_skip_when_not_deployed(self):
        resp = mock.Mock(status_code=404)
        resp.json.return_value = {"detail": "Not found."}  # Django HTML 404 / non-API shape
        resp.json.side_effect = ValueError
        report = outbox_mod.run(self._ctx(resp))
        assert any(r.status == "SKIP" and "not deployed" in r.detail for r in report.results)

    def test_skip_when_no_mailbox_connected(self):
        resp = mock.Mock(status_code=404)
        resp.json.return_value = {"error": {"code": "mailbox_not_connected", "detail": "x"}}
        report = outbox_mod.run(self._ctx(resp))
        assert report.failed is False
        assert any("connect a mailbox" in r.detail for r in report.results)

    def test_secrets_never_in_payload(self):
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"provider": "smtp", "email_address": "a@b.c",
                                  "status": "connected", "password": "leak!"}
        report = outbox_mod.run(self._ctx(resp))
        assert any(r.status == "FAIL" and "secret" in r.name for r in report.results)
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

```python
# apps/common/verify/outbox.py
"""outbox module — mailbox endpoint shape + optional real test send.

Requires the authenticated session established by auth-otp; without it every
check SKIPs (the API answers 401).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apps.common.verify.base import (
    OTP_WAIT, TIMEOUT, ModuleReport, VerifyContext, failed, passed, skipped,
)
from apps.common.verify.api_gate import is_error_shape
from apps.common.verify.auth_otp import csrf_headers
from apps.common.verify.gmail import GmailClient

SECRET_KEYS = {"password", "credentials", "credentials_encrypted", "username", "refresh_token"}
TEST_SUBJECT_MARKER = "findajob.pro"  # subject of send_test_email; substring match


def run(ctx: VerifyContext) -> ModuleReport:
    results = []
    resp = ctx.session.get(ctx.api("/outbox/mailbox/"), timeout=TIMEOUT)
    try:
        payload = resp.json()
    except ValueError:
        payload = None

    if resp.status_code == 401:
        results.append(skipped("mailbox endpoint", "no authenticated session — see auth-otp"))
        return ModuleReport("outbox", results)
    if resp.status_code == 404 and not is_error_shape(payload):
        results.append(skipped("mailbox endpoint", "not deployed on this target"))
        return ModuleReport("outbox", results)
    if resp.status_code == 404 and payload["error"]["code"] == "mailbox_not_connected":
        results.append(skipped("test send via user SMTP",
                               "connect a mailbox in the workspace to enable"))
        return ModuleReport("outbox", results)
    if resp.status_code != 200:
        results.append(failed("mailbox endpoint", f"HTTP {resp.status_code}: {resp.text[:120]}"))
        return ModuleReport("outbox", results)

    results.append(passed("mailbox endpoint → 200"))
    leaked = SECRET_KEYS & set(payload or {})
    results.append(failed("no secrets in payload", f"leaked keys: {sorted(leaked)}") if leaked
                   else passed("no secrets in payload"))
    if (payload or {}).get("status") != "connected":
        results.append(skipped("test send via user SMTP", f"mailbox status={payload.get('status')}"))
        return ModuleReport("outbox", results)

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    resp = ctx.session.post(ctx.api("/outbox/mailbox/test/"),
                            headers=csrf_headers(ctx.session, ctx.target), timeout=TIMEOUT)
    if resp.status_code not in (200, 202):
        results.append(failed("test send accepted", f"HTTP {resp.status_code}: {resp.text[:120]}"))
        return ModuleReport("outbox", results)
    results.append(passed("test send accepted"))

    user, pw = ctx.env.get("VERIFY_GMAIL_USER"), ctx.env.get("VERIFY_GMAIL_APP_PASSWORD")
    if not user or not pw:
        results.append(skipped("test mail in Inbox", "Gmail credentials unset"))
        return ModuleReport("outbox", results)
    found = GmailClient(user, pw).wait_for_message(
        subject_contains=TEST_SUBJECT_MARKER, since=since, timeout=OTP_WAIT)
    if found is None:
        results.append(failed("test mail in Inbox", f"nothing within {OTP_WAIT}s"))
    elif found.folder != "INBOX":
        results.append(failed("test mail in Inbox", f"found in {found.folder}"))
    else:
        results.append(passed("test mail in Inbox"))
    return ModuleReport("outbox", results)
```

Before committing, read `apps/outbox/services.py::send_test_email` and set `TEST_SUBJECT_MARKER` to a substring of the **actual** test-mail subject (and confirm the test mail is addressed to the mailbox owner — if it goes elsewhere, the Inbox check must SKIP with a note instead).

Register: `MODULES["outbox"] = outbox.run`.

- [ ] **Step 4: Run tests → PASS**
- [ ] **Step 5: Commit** — `feat(verify): outbox module (shape, secret hygiene, optional real test send)`

---

### Task 8: `automation` + `n8n` modules, registry completion

**Files:**
- Create: `apps/common/verify/automation.py`
- Create: `apps/common/verify/n8n.py`
- Modify: `apps/common/verify/__init__.py` (register both, then `MODULES["logout"] = auth_otp.finish` last)
- Test: `tests/common/test_verify_platform.py` (append)

**Interfaces:**
- Consumes: `VerifyContext`; n8n env `N8N_VERIFY_URL`, `N8N_VERIFY_API_KEY`.
- Produces: `MODULES["automation"]`, `MODULES["n8n"]`, `MODULES["logout"]`. The automation panel is **admin-only** at `/admin/automation/` — the external check verifies it is *gated* (anonymous request must NOT get the panel), not that it renders.

- [ ] **Step 1: Write the failing tests**

```python
from apps.common.verify import automation as automation_mod, n8n as n8n_mod


class TestAutomation:
    def test_gated_is_pass(self):
        session = mock.Mock()
        # anonymous hit: Traefik basicAuth 401, or Django admin login redirect
        session.get.return_value = mock.Mock(status_code=401, url="https://x/admin/automation/")
        ctx = VerifyContext(target="https://x", session=session, env={})
        report = automation_mod.run(ctx)
        assert report.failed is False
        assert any(r.status == "PASS" and "gated" in r.name for r in report.results)

    def test_open_panel_is_fail(self):
        session = mock.Mock()
        session.get.return_value = mock.Mock(
            status_code=200, url="https://x/admin/automation/", text="<h1>n8n panel</h1>")
        ctx = VerifyContext(target="https://x", session=session, env={})
        assert automation_mod.run(ctx).failed is True


class TestN8n:
    def test_skips_without_creds(self):
        ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
        report = n8n_mod.run(ctx)
        assert report.failed is False
        assert all(r.status == "SKIP" for r in report.results)

    def test_inactive_workflow_fails(self):
        session = mock.Mock()
        wf_resp = mock.Mock(status_code=200)
        wf_resp.json.return_value = {"data": [
            {"id": "1", "name": "findajob scrape", "active": False}]}
        exec_resp = mock.Mock(status_code=200)
        exec_resp.json.return_value = {"data": []}
        with mock.patch("apps.common.verify.n8n.requests.get",
                        side_effect=[wf_resp, exec_resp]):
            ctx = VerifyContext(target="https://x", session=session,
                                env={"N8N_VERIFY_URL": "https://n8n.example",
                                     "N8N_VERIFY_API_KEY": "k"})
            report = n8n_mod.run(ctx)
        assert report.failed is True
```

- [ ] **Step 2: Run → FAIL**

- [ ] **Step 3: Implement**

```python
# apps/common/verify/automation.py
"""automation module — the n8n admin panel must be GATED for outsiders.

/admin/ sits behind Traefik basicAuth in prod; an anonymous 200 with panel
content means the gate fell off — that is the failure this check exists for.
"""
from __future__ import annotations

from apps.common.verify.base import TIMEOUT, ModuleReport, VerifyContext, failed, passed

PATH = "/admin/automation/"


def run(ctx: VerifyContext) -> ModuleReport:
    import requests
    results = []
    # A FRESH session: the main session may hold an authenticated cookie.
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
```

```python
# apps/common/verify/n8n.py
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
        elif latest.get("status") in ("success", "waiting", None) and not latest.get("stoppedAt") is None or latest.get("status") == "success":
            results.append(passed(f"workflow '{name}' last execution"))
        else:
            results.append(failed(f"workflow '{name}' last execution",
                                  f"status={latest.get('status')}"))
    return ModuleReport("n8n", results)
```

(Simplify the tangled `latest` condition during implementation to: `status == "success"` → PASS, `status in ("error", "crashed")` → FAIL, anything else → SKIP with the status in the detail.)

Final registry in `apps/common/verify/__init__.py`:

```python
from apps.common.verify import (
    api_gate, auth_otp, automation, deliverability, frontend, n8n, outbox,
)

MODULES["frontend"] = frontend.run
MODULES["api-gate"] = api_gate.run
MODULES["auth-otp"] = auth_otp.run
MODULES["mail-deliverability"] = deliverability.run
MODULES["outbox"] = outbox.run
MODULES["automation"] = automation.run
MODULES["n8n"] = n8n.run
MODULES["logout"] = auth_otp.finish  # must stay last — tears down the session
```

- [ ] **Step 4: Run tests → PASS**
- [ ] **Step 5: Commit** — `feat(verify): automation-gate + n8n modules, complete registry`

---

### Task 9: Full-suite run, live smoke against prod, docs

**Files:**
- Modify: `CLAUDE.md` (one line under Commands: the new command)
- Test: entire suite + one live run

- [ ] **Step 1: Run the entire test suite**

Run: `cd backend && ../.venv/bin/pytest`
Expected: all pass (574 pre-existing + the new file), ~3s.

- [ ] **Step 2: Live run against prod (no Gmail creds → SKIPs are expected)**

Run: `cd backend && ../.venv/bin/python manage.py verify_platform`
Expected: `frontend` and `api-gate` PASS; `auth-otp` reaches 202 then SKIPs; outbox/automation behave per what is actually deployed; exit 0 unless something is really broken. **If something FAILs here, that is a real prod finding — report it, do not paper over it.**

- [ ] **Step 3: Live run with Gmail credentials (operator supplies the app password)**

Run: `VERIFY_GMAIL_USER=george@rotocon.world VERIFY_GMAIL_APP_PASSWORD=... ../.venv/bin/python manage.py verify_platform`
Expected: full auth-otp + mail-deliverability PASS, OTP mail in INBOX with spf/dkim/dmarc=pass. This step needs the operator to create a Gmail app password (Google Account → Security → 2-Step Verification → App passwords); pause and ask if not provided.

- [ ] **Step 4: Add to CLAUDE.md Commands block**

```bash
../.venv/bin/python manage.py verify_platform   # external smoke of prod; --json, --module <name>
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document verify_platform in CLAUDE.md commands"
```
