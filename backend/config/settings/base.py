"""Base settings shared by every environment.

Stack: Django 6.0 + DRF, PostgreSQL 16, Redis (cache only — sessions stay in the
database so a Redis outage can never log everybody out), static frontend served
by nginx on the same origin under ``/`` with ``/api/`` proxied here.

Django 6.0 notes that are load-bearing here:

* ``DEFAULT_AUTO_FIELD`` defaults to ``BigAutoField`` — the setting is *not*
  emitted on purpose. Adding it back as ``AutoField`` would rewrite every PK.
* ``ADMINS``/``MANAGERS`` are plain address strings, not ``(name, address)``
  tuples (the tuple form is deprecated in 6.0).
* Email helper functions take keyword-only optional arguments in 6.0.
"""

from __future__ import annotations

from pathlib import Path

from config import env

env.load()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

#: ``backend/`` — the directory containing ``manage.py``.
BASE_DIR = Path(__file__).resolve().parents[2]

#: The repository root, which also holds the static frontend in ``web/``.
REPO_ROOT = BASE_DIR.parent


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

#: Only ever used when ``DEBUG`` is on. ``config.checks`` turns its presence
#: into a hard ERROR under ``manage.py check --deploy``.
INSECURE_DEV_SECRET_KEY = "django-insecure-findajob-local-development-key-do-not-ship"

SECRET_KEY = env.get("SECRET_KEY", INSECURE_DEV_SECRET_KEY)

DEBUG = env.get_bool("DEBUG", False)

ALLOWED_HOSTS = env.get_list("ALLOWED_HOSTS", ["localhost", "127.0.0.1"])

CSRF_TRUSTED_ORIGINS = env.get_list("CSRF_TRUSTED_ORIGINS", [])

# Number of reverse proxies in front of gunicorn whose X-Forwarded-For entries
# may be trusted, counted from the RIGHT. Read by
# `apps.legal.request_meta.client_ip` (the address stamped on a consent record)
# and, through it, by `apps.accounts.services.client_ip` (the per-IP rate-limit
# buckets). 0 = trust nothing and use REMOTE_ADDR, which is correct for local
# development and for any deployment reached directly; `config.settings.prod`
# raises it to 2 for the Traefik -> nginx chain. Too low is merely useless, too
# high lets a visitor forge their own address, so the default is the useless one.
TRUSTED_PROXY_COUNT = env.get_int("TRUSTED_PROXY_COUNT", 0)

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# The custom user model is locked in before the first migrate. There is exactly
# one chance at this — see the implementation spec, §2.
AUTH_USER_MODEL = "accounts.User"

# `login_required`/admin redirect target. The auth UI is a static page served by
# nginx from `web/auth.html`, next to `index.html` and `app.html`.
LOGIN_URL = "/auth.html"
LOGIN_REDIRECT_URL = "/app.html"
LOGOUT_REDIRECT_URL = "/index.html"

ADMINS = [address for address in env.get_list("DJANGO_ADMINS", [])]
MANAGERS = ADMINS


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "anymail",
]

# Build order positions 1, 3, 4 (see spec §1). Nothing from position 5+
# (`org-members`, `billing`, `entitlements-quotas`, …) exists yet and nothing
# here may import from it.
LOCAL_APPS = [
    "apps.common",
    "apps.tenancy",
    "apps.accounts",
    "apps.legal",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]


TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
# PostgreSQL in every real environment. The SQLite fallback exists so the test
# suite and a fresh checkout run with zero infrastructure; it is selected only
# when POSTGRES_DB is absent.

if env.get("POSTGRES_DB", ""):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env.get("POSTGRES_DB"),
            "USER": env.get("POSTGRES_USER", "findajob"),
            "PASSWORD": env.get("POSTGRES_PASSWORD", ""),
            "HOST": env.get("POSTGRES_HOST", "db"),
            "PORT": env.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": env.get_int("POSTGRES_CONN_MAX_AGE", 60),
            "CONN_HEALTH_CHECKS": True,
            "OPTIONS": {
                "connect_timeout": env.get_int("POSTGRES_CONNECT_TIMEOUT", 5),
            },
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {"transaction_mode": "IMMEDIATE"},
        }
    }


# ---------------------------------------------------------------------------
# Cache — Redis, cache only.
# ---------------------------------------------------------------------------
# The OTP rate-limit budgets (spec §5) live here. Every call site must degrade
# rather than 500 when Redis is unreachable, so the cache is never on the
# critical path for correctness — only for throttling.

REDIS_URL = env.get("REDIS_URL", "")

if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": env.get("CACHE_KEY_PREFIX", "findajob"),
            "TIMEOUT": env.get_int("CACHE_DEFAULT_TIMEOUT", 300),
            "OPTIONS": {
                "socket_connect_timeout": env.get_int("REDIS_CONNECT_TIMEOUT", 2),
                "socket_timeout": env.get_int("REDIS_SOCKET_TIMEOUT", 2),
            },
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "findajob-locmem",
        }
    }


# ---------------------------------------------------------------------------
# Sessions & CSRF
# ---------------------------------------------------------------------------
# Session cookie, not JWT (spec §1): the frontend and the API are same-origin,
# so a `HttpOnly` cookie is strictly better than a token in localStorage.

SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_NAME = "sessionid"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = env.get_int("SESSION_COOKIE_AGE", 60 * 60 * 24 * 14)
SESSION_EXPIRE_AT_BROWSER_CLOSE = False

CSRF_COOKIE_NAME = "csrftoken"
CSRF_COOKIE_SAMESITE = "Lax"
# A CSRF rejection on /api/… must return the shared JSON envelope, not Django's
# HTML 403 page — `web/api.js` only knows how to read {"error":{code,detail}}.
CSRF_FAILURE_VIEW = "apps.common.errors.csrf_failure"
# MUST stay False: `web/api.js` reads the csrftoken cookie from JavaScript and
# echoes it back in the `X-CSRFToken` header. Setting this True silently breaks
# every anonymous POST (spec §8).
CSRF_COOKIE_HTTPONLY = False
CSRF_USE_SESSIONS = False


# ---------------------------------------------------------------------------
# Password hashing / validation
# ---------------------------------------------------------------------------
# Login is 6-digit email OTP; passwords exist only for Django admin accounts.

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
     "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Internationalisation — EN default, DE, RO
# ---------------------------------------------------------------------------
# The DE legal documents are the binding ones (spec §10), but the product's
# default UI language is English.

LANGUAGE_CODE = "en"

LANGUAGES = [
    ("en", "English"),
    ("de", "Deutsch"),
    ("ro", "Română"),
]

#: Machine-readable allowlist for anything that accepts a `lang` value.
SUPPORTED_LANGUAGE_CODES = tuple(code for code, _label in LANGUAGES)

LOCALE_PATHS = [BASE_DIR / "locale"]

LANGUAGE_COOKIE_NAME = "findajob_lang"
LANGUAGE_COOKIE_HTTPONLY = False
LANGUAGE_COOKIE_SAMESITE = "Lax"

USE_I18N = True
USE_TZ = True
TIME_ZONE = "UTC"


# ---------------------------------------------------------------------------
# Static & media
# ---------------------------------------------------------------------------

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = Path(env.get("MEDIA_ROOT", str(BASE_DIR / "media")))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Uploaded candidate documents are personal data. Keep them off the world-readable
# default and never serve MEDIA_ROOT directly from nginx.
FILE_UPLOAD_PERMISSIONS = 0o600
FILE_UPLOAD_DIRECTORY_PERMISSIONS = 0o700
DATA_UPLOAD_MAX_MEMORY_SIZE = env.get_int("DATA_UPLOAD_MAX_MEMORY_SIZE", 5 * 1024 * 1024)
DATA_UPLOAD_MAX_NUMBER_FIELDS = 200


# ---------------------------------------------------------------------------
# Email — Brevo SMTP relay (spec §1, §6.1)
# ---------------------------------------------------------------------------
# The OTP send path is synchronous inside the request with a hard timeout
# (spec §1): no queue means no plaintext code sitting on Redis and no
# "worker down = nobody can log in".
#
# Transport is the Brevo SMTP RELAY with Django's built-in SMTP backend, NOT the
# Brevo HTTP API. Measured 2026-08-01: the API key is valid but Brevo enforces an
# IP allowlist and rejects both this VPS and the developer laptop
# ("unrecognised IP address"); the VPS egresses over IPv6, so allowlisting its
# IPv4 alone fixes nothing. SMTP authenticates by credential, not source IP, and
# is already proven from this host. The API key stays configured below for the
# bounce/complaint webhook only — never for the login path.

EMAIL_BACKEND = env.get(
    "EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend"
)

EMAIL_HOST = env.get("EMAIL_HOST", "smtp-relay.brevo.com")
EMAIL_PORT = env.get_int("EMAIL_PORT", 587)
EMAIL_USE_TLS = env.get_bool("EMAIL_USE_TLS", True)
EMAIL_USE_SSL = env.get_bool("EMAIL_USE_SSL", False)
#: Brevo SMTP key — generated in the dashboard, NOT the `xkeysib-…` API key.
EMAIL_HOST_USER = env.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env.get("EMAIL_HOST_PASSWORD", "")

#: Hard wall-clock budget for one send, in seconds. Django's SMTP backend passes
#: this to the socket; the console/locmem backends ignore it.
EMAIL_TIMEOUT = env.get_int("EMAIL_TIMEOUT", 5)

DEFAULT_FROM_EMAIL = env.get("DEFAULT_FROM_EMAIL", "findajob.pro <noreply@findajob.pro>")
SERVER_EMAIL = env.get("SERVER_EMAIL", DEFAULT_FROM_EMAIL)

ANYMAIL = {
    "BREVO_API_KEY": env.get("BREVO_API_KEY", ""),
    # Signature secret for POST /webhooks/email/brevo/ (hard bounces and
    # complaints -> accounts.SuppressedAddress, spec §6).
    "WEBHOOK_SECRET": env.get("ANYMAIL_WEBHOOK_SECRET", ""),
}

#: Set to True *only* by config.settings.test, where Django's test runner forces
#: the locmem backend and DEBUG=False. See config.checks.
EMAIL_CHECK_EXEMPT = False


# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
# The gate is the API (spec §7): `index.html` / `app.html` stay publicly served
# by nginx so the demo keeps working, so every route that touches real data has
# to authenticate here. `IsAuthenticated` as the *default* permission means a
# new view is protected unless its author opts out explicitly; `auth/*`,
# `legal/*` and `i18n/*` are the only surfaces allowed to do so.

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "apps.common.authentication.CookieSessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    # JSON only. The browsable API is added back in config.settings.dev.
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    "DEFAULT_PARSER_CLASSES": [
        "rest_framework.parsers.JSONParser",
    ],
    "EXCEPTION_HANDLER": "apps.common.errors.exception_handler",
    "UNAUTHENTICATED_USER": "django.contrib.auth.models.AnonymousUser",
    "DEFAULT_VERSIONING_CLASS": None,
    "COERCE_DECIMAL_TO_STRING": True,
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# Nothing here may ever receive a plaintext OTP code: spec §4 forbids logging
# it, and `apps.common.logging.RedactingFilter` is the backstop.

LOG_LEVEL = env.get("LOG_LEVEL", "INFO").upper()

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "redact_secrets": {"()": "apps.common.logging.RedactingFilter"},
    },
    "formatters": {
        "standard": {
            "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "filters": ["redact_secrets"],
        },
    },
    "root": {"handlers": ["console"], "level": LOG_LEVEL},
    "loggers": {
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "findajob": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}


# ---------------------------------------------------------------------------
# Security defaults (hardened further in config.settings.prod)
# ---------------------------------------------------------------------------

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"

# NOTE: TRUSTED_PROXY_COUNT is defined once, in the Core block near the top of
# this file, and overridden in `config.settings.prod`. Both call sites count
# from the RIGHT of X-Forwarded-For, because nginx proxies with
# $proxy_add_x_forwarded_for and the left-most entry is therefore whatever the
# visitor invented.

DEFAULT_EXCEPTION_REPORTER_FILTER = (
    "django.views.debug.SafeExceptionReporterFilter"
)
