"""Accounts URLs, mounted at ``/api/v1/auth/`` by ``config.urls``.

The four routes in spec §3, plus ``device-login/`` — an additive fifth route
owned by ``docs/2026-08-22-trusted-device-spec.md`` §7, which records why it is
a separate endpoint rather than a branch inside ``request-code/``. Nothing else
belongs here. In particular there is no ``me/`` and no ``session/`` — the single
session endpoint is ``GET /api/v1/me/``, owned by ``apps.tenancy``.

The trailing slashes are load-bearing: ``web/api.js``'s ``ENDPOINTS`` map spells
every path with one, and ``APPEND_SLASH`` cannot rescue a POST (Django refuses
to redirect a request with a body).
"""

from __future__ import annotations

from django.urls import path

from apps.accounts.views import (
    CsrfView,
    DeviceLoginView,
    LogoutView,
    RequestCodeView,
    VerifyCodeView,
)

app_name = "accounts"

urlpatterns = [
    path("csrf/", CsrfView.as_view(), name="csrf"),
    path("request-code/", RequestCodeView.as_view(), name="request-code"),
    path("verify-code/", VerifyCodeView.as_view(), name="verify-code"),
    path("device-login/", DeviceLoginView.as_view(), name="device-login"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
