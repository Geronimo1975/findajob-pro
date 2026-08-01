"""Production settings — findajob.pro, Docker Compose behind Traefik.

TLS terminates at Traefik, so ``SECURE_PROXY_SSL_HEADER`` is mandatory: without
it Django believes every request is plain http and refuses to set the secure
cookies it just marked secure.
"""

from __future__ import annotations

from django.core.exceptions import ImproperlyConfigured

from config import env
from config.settings.base import *  # noqa: F401,F403
from config.settings.base import (
    BASE_DIR,
    INSECURE_DEV_SECRET_KEY,
    SECRET_KEY,
    STORAGES,
)

DEBUG = False

# ---------------------------------------------------------------------------
# Fail loudly on a missing/borrowed secret key rather than silently running on
# a published default. The old platform shipped stock template keys for months.
# ---------------------------------------------------------------------------
if not SECRET_KEY or SECRET_KEY == INSECURE_DEV_SECRET_KEY:
    raise ImproperlyConfigured(
        "SECRET_KEY must be set to a real, unique value in production. "
        "Generate one with: python -c "
        "'from django.core.management.utils import get_random_secret_key as g; print(g())'"
    )

ALLOWED_HOSTS = env.get_list("ALLOWED_HOSTS", [])
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must be set in production.")

CSRF_TRUSTED_ORIGINS = env.get_list("CSRF_TRUSTED_ORIGINS", [])
if not CSRF_TRUSTED_ORIGINS:
    raise ImproperlyConfigured(
        "CSRF_TRUSTED_ORIGINS must list the https origins the browser uses, "
        "e.g. https://findajob.pro"
    )


# ---------------------------------------------------------------------------
# Transport / proxy
# ---------------------------------------------------------------------------

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = env.get_bool("USE_X_FORWARDED_HOST", False)
SECURE_SSL_REDIRECT = env.get_bool("SECURE_SSL_REDIRECT", True)

SECURE_HSTS_SECONDS = env.get_int("SECURE_HSTS_SECONDS", 31_536_000)  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.get_bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", True)
SECURE_HSTS_PRELOAD = env.get_bool("SECURE_HSTS_PRELOAD", True)

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"


# ---------------------------------------------------------------------------
# Cookies
# ---------------------------------------------------------------------------

SESSION_COOKIE_SECURE = True
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

CSRF_COOKIE_SECURE = True
CSRF_COOKIE_SAMESITE = "Lax"
# Stays False on purpose: web/api.js reads this cookie to build the
# X-CSRFToken header (spec §8).
CSRF_COOKIE_HTTPONLY = False

LANGUAGE_COOKIE_SECURE = True


# ---------------------------------------------------------------------------
# Email — real delivery or nothing. config.checks turns a console backend or an
# empty Brevo key into a boot-time ERROR (spec §6).
# ---------------------------------------------------------------------------

EMAIL_BACKEND = env.get("EMAIL_BACKEND", "anymail.backends.brevo.EmailBackend")


# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
# Only Django admin/DRF assets are served from here; the product UI is static
# files served directly by nginx from `web/`.

STORAGES = {
    **STORAGES,
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage",
    },
}

STATIC_ROOT = BASE_DIR / "staticfiles"
