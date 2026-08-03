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
CONNECT_TIMEOUT = 30
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
        conn = imaplib.IMAP4_SSL("imap.gmail.com", timeout=CONNECT_TIMEOUT)
        conn.login(self.user, self.app_password)
        return conn

    def _search(self, conn, folder: str, subject_contains: str, since: datetime):
        # SINCE is date-granular; the caller's subject filter plus newest-first
        # ordering keeps stale same-day mails from matching before fresh ones.
        if conn.select(f'"{folder}"', readonly=True)[0] != "OK":
            return None
        date = since.strftime("%d-%b-%Y")
        # Escape backslashes and quotes in subject_contains for IMAP protocol
        escaped = subject_contains.replace("\\", "\\\\").replace('"', '\\"')
        status, data = conn.search(None, "SINCE", date, "SUBJECT", f'"{escaped}"')
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
