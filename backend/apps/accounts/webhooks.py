"""Brevo bounce / complaint callback -> ``SuppressedAddress`` (spec §6).

``POST /webhooks/email/brevo/`` (mounted by :mod:`apps.accounts.webhook_urls`).

Why this endpoint has to exist at all: ``request-code`` is **branch-free**, so
it will happily mail addresses that never signed up — that is the price of not
being an account-existence oracle. Left unabsorbed, the resulting hard bounces
and spam complaints cost the sending domain its reputation, and the first
symptom is that *nobody* can log in. The suppression list is what pays that
price down.

Two decisions here are deliberate and go against the library's defaults.

**Fail closed with no secret.** ``anymail``'s basic-auth mixin only emits a
warning when ``ANYMAIL["WEBHOOK_SECRET"]`` is unset, and then accepts every
request — leaving an unauthenticated endpoint that writes to a table whose rows
*block people from logging in*. :class:`BrevoTrackingView` refuses to process
anything until a secret is configured. ``config.checks`` already warns about the
same condition at boot (``findajob.W001``).

**Soft bounces never suppress.** Anymail normalises Brevo's ``hard_bounce`` and
``soft_bounce`` to the same ``EventType.BOUNCED``, but the consequences could not
be more different: a soft bounce is a full mailbox or a greylisting provider —
exactly the situation spec §4 says German providers put us in — and suppressing
it would permanently lock a real user out of their own account. The raw Brevo
event name is therefore consulted, and anything ambiguous is *not* suppressed.
Removing a suppression is a manual act in Django admin, so the asymmetry is
correct: over-suppressing is a silent lockout, under-suppressing is a bounce.
"""

from __future__ import annotations

import logging

from django.dispatch import receiver
from django.http import JsonResponse

from anymail.signals import EventType, tracking
from anymail.webhooks.brevo import BrevoTrackingWebhookView

from apps.accounts.models import SuppressedAddress, SuppressionReason
from apps.common.errors import ErrorCode, envelope

logger = logging.getLogger("findajob.accounts.webhooks")

#: Raw Brevo event names that justify never mailing an address again, and the
#: reason each is recorded under. Everything absent from this map — notably
#: ``soft_bounce``, ``deferred``, ``delivered``, and every engagement event — is
#: ignored.
BREVO_SUPPRESSING_EVENTS = {
    "hard_bounce": SuppressionReason.HARD_BOUNCE,
    "invalid_email": SuppressionReason.HARD_BOUNCE,
    "blocked": SuppressionReason.BLOCKED,
    "spam": SuppressionReason.COMPLAINT,
    "complaint": SuppressionReason.COMPLAINT,
    "unsubscribe": SuppressionReason.UNSUBSCRIBE,
    "unsubscribed": SuppressionReason.UNSUBSCRIBE,
    "list_addition": None,  # explicitly not a suppression; listed to be greppable
}

#: Fallback when the raw event name is missing. ``BOUNCED`` is absent on
#: purpose: without the raw name there is no way to tell a hard bounce from a
#: soft one, and guessing wrong locks a user out.
NORMALISED_SUPPRESSING_EVENTS = {
    EventType.COMPLAINED: SuppressionReason.COMPLAINT,
    EventType.REJECTED: SuppressionReason.BLOCKED,
    EventType.UNSUBSCRIBED: SuppressionReason.UNSUBSCRIBE,
}


class BrevoTrackingView(BrevoTrackingWebhookView):
    """Anymail's Brevo tracking webhook, but refusing to run unauthenticated."""

    # We do our own, louder check below; the library's warning would fire on
    # every request without changing the outcome.
    warn_if_no_basic_auth = False

    def post(self, request, *args, **kwargs):
        if not self.basic_auth:
            logger.error(
                "Rejecting a Brevo webhook call: ANYMAIL['WEBHOOK_SECRET'] is "
                "not configured, so the request cannot be authenticated."
            )
            return JsonResponse(
                envelope(
                    ErrorCode.SERVICE_UNAVAILABLE,
                    "Webhook receiver is not configured.",
                ),
                status=503,
            )
        return super().post(request, *args, **kwargs)


def suppression_for(event) -> str | None:
    """The reason to suppress on, or ``None`` to ignore the event."""
    raw = getattr(event, "esp_event", None)
    name = ""
    if isinstance(raw, dict):
        name = str(raw.get("event", "")).strip().lower()
    if name:
        return BREVO_SUPPRESSING_EVENTS.get(name)
    return NORMALISED_SUPPRESSING_EVENTS.get(getattr(event, "event_type", None))


@receiver(tracking, dispatch_uid="findajob.accounts.brevo_tracking")
def handle_tracking_event(sender, event, esp_name, **kwargs) -> None:
    """Record hard bounces and complaints. Never raises.

    An exception here would make Anymail return a 500 to Brevo, which retries —
    so a bug in this receiver would turn into a retry storm against the app.
    """
    try:
        reason = suppression_for(event)
        if reason is None:
            return
        recipient = getattr(event, "recipient", "") or ""
        if not recipient:
            logger.warning("%s tracking event carried no recipient.", esp_name)
            return
        SuppressedAddress.suppress(recipient, reason)
        # The address itself is personal data and the log is not the right place
        # for it; the row in the database is.
        logger.info("Suppressed an address after a %s %s event.", esp_name, reason)
    except Exception:  # noqa: BLE001 - see docstring
        logger.exception("Failed to process an %s tracking event.", esp_name)
