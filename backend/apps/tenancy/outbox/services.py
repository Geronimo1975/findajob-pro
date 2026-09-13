"""SMTP verification and test-send for a user's mailbox.

Both operate on the *user's* SMTP server with the user's credentials — this is
deliberately not Django's configured email backend (that one is Brevo and is
transactional-only, per the outbox spec).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Any

#: One budget for the whole SMTP exchange, mirroring the 5s auth-mail budget.
SMTP_TIMEOUT_SECONDS = 5


class SmtpCheckError(Exception):
    """Verification failed; ``code`` is a machine-readable reason."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


def _connect(smtp_config: dict[str, Any]) -> smtplib.SMTP:
    host = smtp_config["host"]
    port = int(smtp_config["port"])
    use_tls = bool(smtp_config.get("use_tls", True))
    try:
        if port == 465:
            connection = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SECONDS)
        else:
            connection = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS)
            if use_tls:
                connection.starttls()
        connection.login(smtp_config["username"], smtp_config["password"])
        return connection
    except smtplib.SMTPAuthenticationError as exc:
        raise SmtpCheckError(
            "auth_failed", "The mail server rejected the username or password."
        ) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise SmtpCheckError(
            "smtp_unreachable", "Could not reach the mail server."
        ) from exc


def verify_smtp(smtp_config: dict[str, Any]) -> None:
    """Log in and disconnect. Sends nothing. Raises :class:`SmtpCheckError`."""
    connection = _connect(smtp_config)
    try:
        connection.quit()
    except smtplib.SMTPException:
        pass


def send_test_email(
    smtp_config: dict[str, Any], *, from_address: str, display_name: str
) -> None:
    """Send a short test mail from the mailbox to itself."""
    message = EmailMessage()
    message["Subject"] = "findajob.pro — mailbox test"
    message["From"] = (
        f"{display_name} <{from_address}>" if display_name else from_address
    )
    message["To"] = from_address
    message.set_content(
        "Your mailbox is connected to findajob.pro.\n"
        "Application emails will be sent from this address after your review."
    )
    connection = _connect(smtp_config)
    try:
        connection.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise SmtpCheckError("send_failed", "The test email could not be sent.") from exc
    finally:
        try:
            connection.quit()
        except smtplib.SMTPException:
            pass
