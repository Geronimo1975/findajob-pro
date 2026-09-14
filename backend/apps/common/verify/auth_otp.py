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
    # No default on purpose: request-code sends a REAL OTP to whatever address
    # this is, so a hardcoded fallback mails a real person on every run.
    test_email = ctx.env.get("VERIFY_TEST_EMAIL")
    if not test_email:
        return ModuleReport("auth-otp", [skipped(
            "request-code → 202", "set VERIFY_TEST_EMAIL to a mailbox you own to enable")])
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
