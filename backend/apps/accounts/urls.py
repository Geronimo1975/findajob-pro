"""Accounts URLs, mounted at ``/api/v1/auth/`` by ``config.urls``.

Exactly the four routes in spec §3 and nothing else. In particular there is no
``me/`` and no ``session/`` here — the single session endpoint is
``GET /api/v1/me/``, owned by ``apps.tenancy``.

The trailing slashes are load-bearing: ``web/api.js``'s ``ENDPOINTS`` map spells
every path with one, and ``APPEND_SLASH`` cannot rescue a POST (Django refuses
to redirect a request with a body).
"""

from __future__ import annotations

from django.urls import path

from apps.accounts.views import CsrfView, LogoutView, RequestCodeView, VerifyCodeView

app_name = "accounts"

urlpatterns = [
    path("csrf/", CsrfView.as_view(), name="csrf"),
    path("request-code/", RequestCodeView.as_view(), name="request-code"),
    path("verify-code/", VerifyCodeView.as_view(), name="verify-code"),
    path("logout/", LogoutView.as_view(), name="logout"),
]
