"""Local development settings.

Never used on the VPS. ``DEBUG`` defaults to on here; the boot-time email check
in ``config.checks`` is inert while ``DEBUG`` is True, which is exactly why it
has to be a check and not a comment.
"""

from __future__ import annotations

from config import env
from config.settings.base import *  # noqa: F401,F403
from config.settings.base import INSTALLED_APPS, REST_FRAMEWORK

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

# Codes land in the runserver console during development.
EMAIL_BACKEND = env.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)

# Cookies must stay readable over plain http locally.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0
