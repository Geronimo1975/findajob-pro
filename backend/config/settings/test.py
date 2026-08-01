"""Test settings — zero infrastructure required.

SQLite, in-memory cache, in-memory email. Both ``manage.py test`` and
``pytest`` (via ``pytest.ini``) point here.

Django's test runner forces ``DEBUG = False`` and swaps ``EMAIL_BACKEND`` to
the locmem backend *before* system checks run, which would otherwise trip the
production email check in ``config.checks``. ``EMAIL_CHECK_EXEMPT`` is the one
documented way out, and only this module sets it.
"""

from __future__ import annotations

from config.settings.base import *  # noqa: F401,F403

DEBUG = False

SECRET_KEY = "django-insecure-findajob-test-key-not-used-outside-the-test-suite"

ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]
CSRF_TRUSTED_ORIGINS = ["http://testserver", "http://localhost"]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
        "TEST": {"NAME": ":memory:"},
    }
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "findajob-tests",
    }
}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

# See the module docstring: the runner's forced locmem backend under DEBUG=False
# is not a misconfiguration.
EMAIL_CHECK_EXEMPT = True

# Fast, deterministic hashing — the product logs in with OTP codes, not
# passwords, so nothing of value is being weakened here.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Cookies must work over the test client's plain http.
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_SSL_REDIRECT = False
SECURE_HSTS_SECONDS = 0

LANGUAGE_CODE = "en"

# Keep test output readable; individual tests can raise the level again.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"redact_secrets": {"()": "apps.common.logging.RedactingFilter"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "filters": ["redact_secrets"]},
    },
    "root": {"handlers": ["console"], "level": "CRITICAL"},
    "loggers": {
        # Django's DEFAULT_LOGGING is merged in before this dict, so `django`
        # and `django.request` need muting explicitly or every expected 401/404
        # in the suite prints a warning.
        "django": {"handlers": ["console"], "level": "CRITICAL", "propagate": False},
        "findajob": {"handlers": ["console"], "level": "CRITICAL", "propagate": False},
    },
}
