"""Delivery of the login code — synchronous, bounded, and loud on failure.

Implementation spec §1::

    Send path: **Synchronous**, inside the request, 5 s timeout. Kills three
    blockers at once: no plaintext code on a Redis queue, no "worker down =
    nobody logs in", no green-UI-zero-delivery.

Consequences that are easy to get wrong and are handled here:

**The timeout has to be passed explicitly.** ``django-anymail``'s requests
backend defaults to a 30-second ``requests_timeout`` and does **not** read
``EMAIL_TIMEOUT``; Django's SMTP backend does read it, but only as a per-socket
timeout. Passing ``timeout=settings.EMAIL_TIMEOUT`` into ``get_connection()``
is what actually bounds the login request, and it works for both backends
(``BaseEmailBackend`` swallows unknown kwargs, Anymail's requests backend pops
``timeout`` by name). Note the residual: for SMTP this bounds each socket
operation rather than the whole conversation.

**A failure must never look like success.** Anything that goes wrong raises
:class:`~apps.common.errors.DeliveryUnavailable` → ``503
delivery_unavailable``. Returning a green ``202`` while the code went nowhere
is the single worst outcome for this endpoint, because the user waits for an
email that does not exist and has no way to tell.

**Suppressed addresses are skipped, not failed** (spec §6). ``request-code`` is
branch-free, so it will mail addresses that never asked for anything; hard
bounces and complaints land in ``SuppressedAddress`` and are silently skipped
here — the endpoint still answers ``202``, because behaving differently would
turn the suppression list into an account-existence oracle.

**No template files.** The three languages live in this module as plain strings.
A missing template directory or a typo in a template name would raise inside the
login request, i.e. it would be a login outage; and templates buy nothing here
because the message is six lines long. The code is interpolated at send time and
never touches a logger.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.utils.html import escape

from apps.common.errors import DeliveryUnavailable

logger = logging.getLogger("findajob.accounts.mail")

DEFAULT_LANG = "en"


# ---------------------------------------------------------------------------
# Copy — EN / DE / RO, added in all three at once (spec §11)
# ---------------------------------------------------------------------------
# `{code}` and `{minutes}` are the only placeholders. Keep the code on its own
# line: Gmail, Outlook and Apple Mail all surface a lone 6-digit line as a
# one-tap autofill suggestion.

_COPY: dict[str, dict[str, str]] = {
    "en": {
        "subject": "{code} is your findajob.pro login code",
        "greeting": "Here is your login code for findajob.pro:",
        "validity": "The code is valid for {minutes} minutes and can be used once.",
        "ignore": (
            "If you did not request this code, you can ignore this email — "
            "nobody can sign in without it."
        ),
        "signoff": "findajob.pro",
        "code_label": "Login code",
    },
    "de": {
        "subject": "{code} ist Ihr Anmeldecode für findajob.pro",
        "greeting": "Hier ist Ihr Anmeldecode für findajob.pro:",
        "validity": (
            "Der Code ist {minutes} Minuten gültig und kann einmal "
            "verwendet werden."
        ),
        "ignore": (
            "Falls Sie diesen Code nicht angefordert haben, können Sie "
            "diese E-Mail ignorieren — ohne den Code kann sich niemand "
            "anmelden."
        ),
        "signoff": "findajob.pro",
        "code_label": "Anmeldecode",
    },
    "ro": {
        "subject": "{code} este codul dumneavoastră de autentificare findajob.pro",
        "greeting": "Acesta este codul dumneavoastră de autentificare pentru findajob.pro:",
        "validity": (
            "Codul este valabil {minutes} minute și poate fi folosit o "
            "singură dată."
        ),
        "ignore": (
            "Dacă nu ați solicitat acest cod, puteți ignora acest "
            "e-mail — nimeni nu se poate autentifica fără el."
        ),
        "signoff": "findajob.pro",
        "code_label": "Cod de autentificare",
    },
}


def copy_for(lang: str | None) -> dict[str, str]:
    """Message strings for ``lang``, falling back to English."""
    key = (lang or "").strip().lower()
    if key in _COPY:
        return _COPY[key]
    base = key.split("-", 1)[0]
    return _COPY.get(base, _COPY[DEFAULT_LANG])


def _plain_body(strings: dict[str, str], code: str, minutes: int) -> str:
    return (
        f"{strings['greeting']}\n"
        f"\n"
        f"{code}\n"
        f"\n"
        f"{strings['validity'].format(minutes=minutes)}\n"
        f"{strings['ignore']}\n"
        f"\n"
        f"{strings['signoff']}\n"
    )


def _html_body(strings: dict[str, str], code: str, minutes: int) -> str:
    # Inline styles only: every meaningful email client strips <style> blocks.
    # `escape()` on the code is belt-and-braces — it is six digits from
    # `secrets.randbelow` and cannot contain markup — but the greeting strings
    # are translator-editable and one of them will eventually contain an "&".
    safe_code = escape(code)
    return (
        '<div style="font-family:Inter,Helvetica,Arial,sans-serif;'
        'background:#101010;color:#e8e8e8;padding:32px">'
        f'<p style="margin:0 0 20px;font-size:15px;line-height:1.6">'
        f"{escape(strings['greeting'])}</p>"
        '<p style="margin:0 0 8px;font-size:11px;letter-spacing:.08em;'
        'text-transform:uppercase;color:#8a8a8a">'
        f"{escape(strings['code_label'])}</p>"
        '<p style="margin:0 0 24px;font-family:\'JetBrains Mono\','
        "'SFMono-Regular',Consolas,monospace;font-size:34px;letter-spacing:.32em;"
        'font-weight:600;color:#00d992">'
        f"{safe_code}</p>"
        '<p style="margin:0 0 6px;font-size:13px;line-height:1.6;color:#b6b6b6">'
        f"{escape(strings['validity'].format(minutes=minutes))}</p>"
        '<p style="margin:0 0 24px;font-size:13px;line-height:1.6;color:#8a8a8a">'
        f"{escape(strings['ignore'])}</p>"
        '<hr style="border:0;border-top:1px solid #3d3a39;margin:0 0 16px">'
        '<p style="margin:0;font-size:12px;color:#8a8a8a">'
        f"{escape(strings['signoff'])}</p>"
        "</div>"
    )


def build_login_code_message(
    *, email: str, code: str, lang: str, expires_in: int, connection=None
) -> EmailMultiAlternatives:
    """Compose the message. Separated from sending so tests can inspect it."""
    strings = copy_for(lang)
    minutes = max(1, int(expires_in) // 60)
    message = EmailMultiAlternatives(
        subject=strings["subject"].format(code=code),
        body=_plain_body(strings, code, minutes),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email],
        connection=connection,
        headers={
            # RFC 3834: stop vacation auto-responders from replying to a
            # no-reply address and, on some providers, from bouncing it.
            "Auto-Submitted": "auto-generated",
            "X-Auto-Response-Suppress": "All",
        },
    )
    message.extra_headers.setdefault("X-Entity-Ref-ID", "findajob-login-code")
    message.attach_alternative(_html_body(strings, code, minutes), "text/html")
    return message


class SendOutcome:
    """Why a ``202`` was returned. Never serialised to the client."""

    __slots__ = ("delivered", "suppressed")

    def __init__(self, *, delivered: bool, suppressed: bool = False) -> None:
        self.delivered = delivered
        self.suppressed = suppressed

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SendOutcome(delivered={self.delivered}, suppressed={self.suppressed})"


def send_login_code(*, email: str, code: str, lang: str, expires_in: int) -> SendOutcome:
    """Deliver one login code, synchronously.

    :raises apps.common.errors.DeliveryUnavailable: on any failure to hand the
        message to the provider. The caller answers ``503 delivery_unavailable``
        and removes the code it had just written, so the user can retry at once.
    """
    from apps.accounts.models import SuppressedAddress

    try:
        suppressed = SuppressedAddress.is_suppressed(email)
    except Exception as exc:  # noqa: BLE001 - a DB hiccup must not block login
        logger.warning("Suppression lookup failed, proceeding with send: %s", exc)
        suppressed = False

    if suppressed:
        # Still a 202 upstream: differing here would leak which addresses have
        # bounced, and a bounced address is usually a real one.
        logger.info("Skipping login code to a suppressed address.")
        return SendOutcome(delivered=False, suppressed=True)

    timeout = getattr(settings, "EMAIL_TIMEOUT", None) or 5

    try:
        connection = get_connection(fail_silently=False, timeout=timeout)
        message = build_login_code_message(
            email=email,
            code=code,
            lang=lang,
            expires_in=expires_in,
            connection=connection,
        )
        sent = message.send(fail_silently=False)
    except Exception as exc:  # noqa: BLE001 - every ESP raises its own hierarchy
        # `exc` is logged without the message body, and the body is the only
        # place the code appears. `apps.common.logging.RedactingFilter` is the
        # backstop if a provider ever echoes it into an error string.
        logger.error(
            "Login code delivery failed via %s: %s",
            getattr(settings, "EMAIL_BACKEND", "?"),
            exc.__class__.__name__,
            exc_info=True,
        )
        raise DeliveryUnavailable() from exc

    if not sent:
        # `send()` returning 0 without raising means the backend accepted the
        # call and delivered nothing — exactly the silent failure spec §1 is
        # about.
        logger.error(
            "Login code delivery reported 0 messages sent via %s.",
            getattr(settings, "EMAIL_BACKEND", "?"),
        )
        raise DeliveryUnavailable()

    return SendOutcome(delivered=True)
