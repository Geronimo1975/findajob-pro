"""Root URLConf.

Everything the browser talks to lives under ``/api/v1/`` (matching
``web/api.js``'s ``export const BASE = '/api/v1'``). nginx proxies ``/api/`` to
this process and serves ``index.html`` / ``app.html`` / ``auth.html`` itself, so
the two are same-origin and the session cookie needs no CORS.

Route ownership (spec §3 is the complete, exclusive endpoint list):

===========================  ==============  ============================
Path                         Owning app      Auth
===========================  ==============  ============================
``/api/v1/auth/…``           apps.accounts   anonymous, ``@csrf_protect``
``/api/v1/legal/…``          apps.legal      anonymous
``/api/v1/consent/…``        apps.legal      session
``/api/v1/i18n/…``           Django          anonymous
``/api/v1/me/``              apps.tenancy    session
===========================  ==============  ============================

There is **no** ``/auth/me/`` and **no** ``/auth/session/``. Everything not
listed as anonymous above is gated by DRF's default ``IsAuthenticated``
permission (spec §7 — the gate is the API, not Traefik).
"""

from __future__ import annotations

import importlib

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

#: Django resolves these by dotted path for URLConf-level 404s and unhandled
#: 500s. They keep the shared ``{"error": {...}}`` envelope for anything under
#: ``/api/`` and defer to Django's own HTML responses everywhere else.
handler400 = "apps.common.errors.api_bad_request"
handler403 = "apps.common.errors.api_permission_denied"
handler404 = "apps.common.errors.api_page_not_found"
handler500 = "apps.common.errors.api_server_error"


def _include_if_present(module_path: str, prefix: str = ""):
    """Mount ``module_path`` only if that module exists.

    ``apps.accounts`` and ``apps.legal`` are built by separate work streams. A
    missing URLConf must not stop the rest of the API from booting mid-build,
    but a *broken* one must still explode: only a ``ModuleNotFoundError`` naming
    this exact module is swallowed, so an ImportError raised from inside the
    URLConf still propagates.
    """
    try:
        importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name == module_path:
            return []
        raise
    return [path(prefix, include(module_path))]


api_v1_patterns = [
    # apps.tenancy — the single session endpoint, GET /api/v1/me/
    path("", include("apps.tenancy.urls")),
    # apps.accounts — /api/v1/auth/csrf|request-code|verify-code|logout/
    *_include_if_present("apps.accounts.urls", "auth/"),
    # apps.legal — versioned legal documents (anonymous) …
    *_include_if_present("apps.legal.urls", "legal/"),
    *_include_if_present("apps.outbox.urls", "outbox/"),
    # apps.jobs — /platforms/ and /jobs/ live at the API root per ENDPOINTS.
    *_include_if_present("apps.jobs.urls", ""),
    # … and the consent ledger, which spec §3/§10 puts at /api/v1/consent/,
    # not under legal/. Same app, different mount point, hence a second module.
    *_include_if_present("apps.legal.consent_urls", "consent/"),
    # Anonymous surface: language switching.
    path("i18n/", include("django.conf.urls.i18n")),
]

urlpatterns = [
    path("api/v1/", include(api_v1_patterns)),
    # Read-only n8n panel. MUST be listed BEFORE `admin.site.urls`:
    # `AdminSite.get_urls` ends in a catch-all, so anything mounted after it is
    # unreachable. The view itself is wrapped in `admin.site.admin_view`, so it
    # carries the identical staff check and admin-login redirect as every other
    # admin page — see apps/automation/admin.py for why it is not a ModelAdmin.
    path("admin/automation/", include("apps.automation.urls")),
    path("admin/", admin.site.urls),
    # Email service provider callbacks (Brevo hard bounces / complaints ->
    # accounts.SuppressedAddress). Mounted outside /api/ because they are
    # machine-to-machine and signature-verified, not session-authenticated.
    *_include_if_present("apps.accounts.webhook_urls", "webhooks/email/"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

# django-debug-toolbar's `__debug__/` tree.
#
# The guard is `INSTALLED_APPS`, not `try: import`. Since 7.0 the toolbar ships
# a model (`debug_toolbar.models.HistoryEntry`), and importing any module that
# reaches it while the app is *not* installed raises `RuntimeError` from
# `ModelBase.__new__` — "doesn't declare an explicit app_label and isn't in
# INSTALLED_APPS" — which is not an ImportError and would not be caught.
#
# `config.settings.dev` is the only settings module that appends the app, and
# it does so only when DEBUG is on and the package is present. So this single
# condition already covers "not installed in production" and "DEBUG off".
# `debug_toolbar_urls()` re-checks DEBUG itself and returns [] — belt and
# braces, at no cost.
if "debug_toolbar" in settings.INSTALLED_APPS:
    from debug_toolbar.toolbar import debug_toolbar_urls

    urlpatterns += debug_toolbar_urls()
