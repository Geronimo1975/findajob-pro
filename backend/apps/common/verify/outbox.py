"""outbox module — mailbox endpoint shape + optional real test send.

Requires the authenticated session established by auth-otp; without it every
check SKIPs (the API answers 401).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apps.common.verify.base import (
    OTP_WAIT, TIMEOUT, ModuleReport, VerifyContext, failed, passed, skipped,
)
from apps.common.verify.auth_otp import csrf_headers
from apps.common.verify.gmail import GmailClient

SECRET_KEYS = {"password", "credentials", "credentials_encrypted", "username", "refresh_token"}
#: substring of send_test_email's actual subject "findajob.pro — mailbox test"
#: (apps/tenancy/outbox/services.py::send_test_email). The mail is sent to the
#: mailbox's own address (message["To"] = from_address). The Inbox check
#: below only runs when that address matches VERIFY_GMAIL_USER — otherwise
#: we'd be polling a Gmail inbox that will never receive the test mail.
TEST_SUBJECT_MARKER = "findajob.pro"


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
    if resp.status_code == 404 and payload and payload.get("error", {}).get("code") == "mailbox_not_connected":
        results.append(skipped("test send via user SMTP",
                               "connect a mailbox in the workspace to enable"))
        return ModuleReport("outbox", results)
    if resp.status_code == 404:
        # handler404 (apps/common/errors.py::api_page_not_found) wraps *every*
        # unmatched /api/ route in the same error shape as a real view's 404,
        # so an unmounted apps.tenancy.outbox.urls (module absent on this deploy
        # target) is indistinguishable from a genuinely missing sub-path other
        # than by the generic "not_found" code used here.
        results.append(skipped("mailbox endpoint", "not deployed on this target"))
        return ModuleReport("outbox", results)
    if resp.status_code != 200:
        results.append(failed("mailbox endpoint", f"HTTP {resp.status_code}: {str(resp.text)[:120]}"))
        return ModuleReport("outbox", results)

    results.append(passed("mailbox endpoint → 200"))
    leaked = SECRET_KEYS & set(payload or {})
    results.append(failed("no secrets in payload", f"leaked keys: {sorted(leaked)}") if leaked
                   else passed("no secrets in payload"))
    if (payload or {}).get("status") != "connected":
        results.append(skipped("test send via user SMTP", f"mailbox status={(payload or {}).get('status')}"))
        return ModuleReport("outbox", results)

    user, pw = ctx.env.get("VERIFY_GMAIL_USER"), ctx.env.get("VERIFY_GMAIL_APP_PASSWORD")
    if not user or not pw:
        results.append(skipped("test mail in Inbox", "Gmail credentials unset"))
        return ModuleReport("outbox", results)

    mailbox_address = (payload or {}).get("email_address", "") or ""
    if mailbox_address.lower() != user.lower():
        results.append(skipped(
            "test mail in Inbox",
            "mailbox address differs from VERIFY_GMAIL_USER — cannot verify inbox delivery",
        ))
        return ModuleReport("outbox", results)

    since = datetime.now(timezone.utc) - timedelta(minutes=1)
    resp = ctx.session.post(ctx.api("/outbox/mailbox/test/"),
                            headers=csrf_headers(ctx.session, ctx.target), timeout=TIMEOUT)
    if resp.status_code not in (200, 202):
        results.append(failed("test send accepted", f"HTTP {resp.status_code}: {str(resp.text)[:120]}"))
        return ModuleReport("outbox", results)
    results.append(passed("test send accepted"))

    found = GmailClient(user, pw).wait_for_message(
        subject_contains=TEST_SUBJECT_MARKER, since=since, timeout=OTP_WAIT)
    if found is None:
        results.append(failed("test mail in Inbox", f"nothing within {OTP_WAIT}s"))
    elif found.folder != "INBOX":
        results.append(failed("test mail in Inbox", f"found in {found.folder}"))
    else:
        results.append(passed("test mail in Inbox"))
    return ModuleReport("outbox", results)
