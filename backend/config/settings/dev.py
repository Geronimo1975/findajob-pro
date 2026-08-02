"""Local development settings.

Never used on the VPS. ``DEBUG`` defaults to on here; the boot-time email check
in ``config.checks`` is inert while ``DEBUG`` is True, which is exactly why it
has to be a check and not a comment.
"""

from __future__ import annotations

import importlib.util

from config import env
from config.settings.base import *  # noqa: F401,F403
from config.settings.base import INSTALLED_APPS, MIDDLEWARE, REST_FRAMEWORK

DEBUG = env.get_bool("DEBUG", True)

ALLOWED_HOSTS = env.get_list(
    "ALLOWED_HOSTS", ["localhost", "127.0.0.1", "[::1]", "testserver"]
)

# Same-origin in production (nginx proxies /api/ to Django), so no CORS package
# is needed. Locally the static shell is usually opened straight off disk or
# from a different port, so the dev CSRF allowlist is a little wider.
CSRF_TRUSTED_ORIGINS = env.get_list(
    "CSRF_TRUSTED_ORIGINS",
    ["http://localhost:8000", "http://127.0.0.1:8000", "http://localhost:5173"],
)

# The browsable API is a development affordance only — production stays JSON-only.
REST_FRAMEWORK = {
    **REST_FRAMEWORK,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

INSTALLED_APPS = [*INSTALLED_APPS]

# --- django-debug-toolbar -------------------------------------------------
#
# Development-only, and installed only if it is actually present: it lives in
# requirements-dev.txt, so a checkout that installed requirements.txt alone
# must still run `manage.py runserver` rather than die on an import. The same
# guard is repeated in config/urls.py for the `__debug__/` mount.
#
# It is deliberately NOT enabled while running tests. `config.settings.test`
# does not import this module at all, so this block never executes there — but
# note that even here it stays off unless DEBUG is on, because the toolbar
# renders into the response body and would corrupt any HTML assertion.
DEBUG_TOOLBAR_INSTALLED = importlib.util.find_spec("debug_toolbar") is not None

if DEBUG and DEBUG_TOOLBAR_INSTALLED:
    INSTALLED_APPS = [*INSTALLED_APPS, "debug_toolbar"]
    # As early as possible, so the timing and SQL panels see the work done by
    # every middleware below them. Ahead of SecurityMiddleware is what the
    # toolbar's own installation docs ask for.
    MIDDLEWARE = [
        "debug_toolbar.middleware.DebugToolbarMiddleware",
        *MIDDLEWARE,
    ]
    # runserver on the host, and the container gateway when the api runs under
    # compose — the toolbar only renders for a request whose REMOTE_ADDR is
    # listed here.
    INTERNAL_IPS = ["127.0.0.1", "::1"]
    DEBUG_TOOLBAR_CONFIG = {
        # The admin is the reason this is installed, and every admin page is a
        # full document — so the default "inject into any text/html response"
        # behaviour is what we want. Keep the toolbar out of API responses,
        # which are JSON and would be corrupted by an injected panel.
        "SHOW_TOOLBAR_CALLBACK": "config.settings.dev.show_toolbar",
        # A changelist that fires 400 queries is exactly the bug being hunted;
        # truncating the SQL panel at the default 0 (unlimited) is right, but
        # the pretty-printer on a large payload is slow enough to notice.
        "ENABLE_STACKTRACES": True,
    }


def show_toolbar(request) -> bool:
    """Render the toolbar on HTML pages from a local address, never on the API.

    The default callback checks ``DEBUG`` and ``INTERNAL_IPS`` only, which would
    also fire for ``/api/v1/me/``. DRF's ``JSONRenderer`` produces
    ``application/json``, so the toolbar's own content-type check would decline
    to inject there anyway — but the browsable API renderer is enabled in this
    settings module and *does* return HTML, and an injected panel inside it is
    noise at best. Excluding the prefix outright is one line and unambiguous.
    """
    if not DEBUG or request.META.get("REMOTE_ADDR") not in INTERNAL_IPS:
        return False
    return not request.path.startswith("/api/")

# Codes land in the runserver console during development.
EMAIL_BACKEND = env.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)

# Cookies must stay readable over plain http locally.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
