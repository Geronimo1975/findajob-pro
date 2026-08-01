"""Public legal routes, mounted at ``/api/v1/legal/`` by ``config.urls``.

The consent routes live in :mod:`apps.legal.consent_urls` because spec §3 puts
them at ``/api/v1/consent/``, not under ``legal/`` — same app, different mount
point, so they cannot share a URLConf module.
"""

from __future__ import annotations

from django.urls import path, re_path

from apps.legal.views import LegalDocumentView, LegalIndexView

app_name = "legal"

urlpatterns = [
    path("", LegalIndexView.as_view(), name="index"),
    # Bounded and explicit: the view rejects anything that is not one of the
    # six known ids, and the pattern keeps a path traversal attempt from ever
    # reaching it.
    re_path(r"^(?P<doc_id>[a-z_]{1,32})/$", LegalDocumentView.as_view(), name="document"),
]
