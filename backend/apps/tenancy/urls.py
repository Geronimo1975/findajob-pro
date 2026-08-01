"""Tenancy URLs, mounted at the root of ``/api/v1/`` by ``config.urls``.

Exactly one route. ``/api/v1/me/`` is the *only* session endpoint in the
product (spec §3) — do not add ``/auth/me/`` or ``/auth/session/`` here or
anywhere else.
"""

from __future__ import annotations

from django.urls import path

from apps.tenancy.views import MeView

app_name = "tenancy"

urlpatterns = [
    path("me/", MeView.as_view(), name="me"),
]
