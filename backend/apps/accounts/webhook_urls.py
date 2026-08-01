"""Email service provider callbacks, mounted at ``/webhooks/email/``.

Deliberately outside ``/api/`` (see ``config.urls``): these are
machine-to-machine, authenticated by a shared secret rather than by a session,
and they must not inherit the API's session/CSRF plumbing.

One route, ``POST /webhooks/email/brevo/`` (spec §6): Brevo posts hard bounces
and spam complaints, and they become
:class:`~apps.accounts.models.SuppressedAddress` rows.
"""

from __future__ import annotations

from django.urls import path

from apps.accounts.webhooks import BrevoTrackingView

app_name = "accounts_webhooks"

urlpatterns = [
    path("brevo/", BrevoTrackingView.as_view(), name="brevo"),
]
