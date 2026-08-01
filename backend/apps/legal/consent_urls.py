"""Consent routes, mounted at ``/api/v1/consent/`` by ``config.urls``.

Separate module from :mod:`apps.legal.urls` only because the two live at
different prefixes. Both are owned by ``apps.legal``.
"""

from __future__ import annotations

from django.urls import path, re_path

from apps.legal.views import ConsentRevokeView, ConsentView

app_name = "legal_consent"

urlpatterns = [
    path("", ConsentView.as_view(), name="consent"),
    re_path(
        r"^(?P<consent_type>[a-z_]{1,32})/$",
        ConsentRevokeView.as_view(),
        name="consent-revoke",
    ),
]
