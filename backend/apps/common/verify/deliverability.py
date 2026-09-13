"""mail-deliverability — proof the OTP mail reached a real Inbox, plus DNS posture."""
from __future__ import annotations

import subprocess

from apps.common.verify.base import ModuleReport, VerifyContext, failed, passed, skipped
from apps.common.verify.gmail import auth_results

DOMAIN = "findajob.pro"
# OTP mail is sent from noreply@mail.findajob.pro (Brevo sending domain), so SPF
# lives on the subdomain; the apex has no MX and no SPF on purpose.
MAIL_DOMAIN = f"mail.{DOMAIN}"


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
    # Note: DKIM selector DNS check is deliberately omitted. Selector names are
    # account-specific (mail._domainkey vs brevo._domainkey); the header check
    # (dkim=pass) already proves signing works end-to-end, so a selector guess
    # would only produce false FAILs. DNS checks stay at SPF + DMARC only.
    try:
        spf = [r for r in dig_txt(MAIL_DOMAIN) if r.startswith("v=spf1")]
        results.append(passed("SPF record published", spf[0]) if spf
                       else failed("SPF record published", f"no v=spf1 TXT on {MAIL_DOMAIN}"))
        dmarc = [r for r in dig_txt(f"_dmarc.{MAIL_DOMAIN}") if r.startswith("v=DMARC1")]
        results.append(passed("DMARC record published", dmarc[0]) if dmarc
                       else failed("DMARC record published", f"no v=DMARC1 TXT on _dmarc.{MAIL_DOMAIN}"))
    except DigError as exc:
        results.append(skipped("DNS SPF/DMARC records", f"dig unavailable: {exc}"))
    return ModuleReport("mail-deliverability", results)
