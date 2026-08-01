"""Log redaction.

Spec §4: the 6-digit OTP code is *"never stored, logged, or put in an audit
payload"*. Discipline at the call site is the primary control; this filter is
the backstop for the paths nobody reviews — a third-party library echoing a
request body, a ``repr()`` of a form, an unexpected traceback frame.

It rewrites ``record.msg`` and ``record.args`` in place, so it must stay cheap:
it only runs a compiled regex substitution over the already-formatted message.
"""

from __future__ import annotations

import logging
import re

REDACTED = "[redacted]"

#: ``Authorization: Bearer abc123``. Needs its own pattern because the value is
#: two whitespace-separated words, which the generic assignment pattern below
#: would truncate — leaving the actual credential in the log line.
_AUTH_HEADER = re.compile(
    r"""(?ix)
    \b(authorization | proxy-authorization)\b
    (\s*[:=]\s*)
    (?: bearer | basic | token | digest | api-?key \s )?
    \s*
    \S+
    """
)

#: ``code=123456``, ``"code": "123456"``, ``otp=123456``, ``password: hunter2`` …
_SENSITIVE_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(
        code | otp | one_time_code | code_hash |
        password | passwd | secret | token | api[_-]?key |
        sessionid | csrftoken
    )
    (["']?\s*[:=]\s*["']?)
    ([^\s,;)"'}\]]+)
    """
)

#: A bare six-digit number sitting next to the word "code" is the OTP itself.
_BARE_OTP = re.compile(r"(?i)\b(code)\b(\W{0,4})(\d{6})\b")


def redact(text: str) -> str:
    """Replace secret-looking values in ``text``."""
    text = _AUTH_HEADER.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _SENSITIVE_ASSIGNMENT.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text
    )
    return _BARE_OTP.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


class RedactingFilter(logging.Filter):
    """Attached to every handler in ``settings.LOGGING``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True
