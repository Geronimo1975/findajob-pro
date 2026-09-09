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
    # A subclass of django.contrib.admin.apps.AdminConfig whose `name` is still
    # "django.contrib.admin" — so the app label, templates, static files, URL
    # namespace and admin system checks are all unchanged. The only thing it
    # changes is `default_site`, which makes `admin.site` a FindajobAdminSite
    # (branding + a deliberate index order). See apps/common/admin_config.py.
    "apps.common.admin_config.FindajobAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
    "anymail",
    # Admin-only, both of them. Neither ships a model or a migration, and
    # neither is imported outside `apps/*/admin.py` and
    # `apps.common.admin_integrations`. They are listed here rather than in a
    # dev-only module because the admin they extend runs in production — that
    # is the whole point of it.
    #
    # Template precedence: `import_export` ships templates under
    # `admin/import_export/`, and `django_object_actions` under
    # `django_object_actions/`. Neither name collides with the Voltagent
    # overrides in `apps.common`, and `apps.common` is pinned ahead of
    # everything anyway (see the note on INSTALLED_APPS below), so the theme
    # keeps winning regardless.
    "import_export",
    "django_object_actions",
]

# Build order positions 1, 3, 4 (see spec §1). Nothing from position 5+
# (`org-members`, `billing`, `entitlements-quotas`, …) exists yet and nothing
# here may import from it.
#
# `apps.common` is NOT in this list — it is pinned ahead of the admin in
# INSTALLED_APPS below. See the note there before moving it back.
LOCAL_APPS = [
    "apps.tenancy",
    "apps.accounts",
    "apps.legal",
    # Model-less, read-only visibility into the n8n automation layer. Adds no
    # migrations and nothing else may import from it.
    "apps.automation",
    # Send-as-user mailbox connections; see docs/2026-08-02-outbox-spec.md.
    "apps.outbox",
    # Platform catalog, scraped listings, applications, documents.
    "apps.jobs",
    # Remembered machines that may skip the OTP round trip; see
    # docs/2026-08-22-trusted-device-spec.md.
    "apps.devices",
]

# ORDER IS LOAD-BEARING, in exactly one place: `apps.common` must precede the
# admin app.
#
# django.template.loaders.app_directories walks INSTALLED_APPS in order and
# returns the FIRST template that matches a name. `apps.common` ships
# apps/common/templates/admin/base_site.html (the Voltagent brand chrome, and
# the <link> that loads admin/css/voltagent-admin.css). Listed after the admin
# entry — which is where it used to sit, inside LOCAL_APPS — Django's own
# django/contrib/admin/templates/admin/base_site.html wins instead, and the
# entire theme silently does nothing: no error, no warning, no log line, just a
# stock blue admin. That is the single most common reason admin overrides
# appear not to work.
#
# Note the admin entry below is `apps.common.admin_config.FindajobAdminConfig`,
# whose AppConfig.name is still "django.contrib.admin" — so it is the admin
# app, at the admin's position, and this ordering rule applies to it verbatim.
#
# Everything else here is alphabetical-by-convenience and carries no meaning.
# `apps.common` owns no models and no management commands, so moving it to the
# front changes nothing but template and staticfiles finder precedence.
# (config/settings/test.py and .../dev.py import this list unchanged.)
INSTALLED_APPS = ["apps.common"] + DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS


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

#: Modes collectstatic must write STATIC_ROOT with. See the note below.
STATIC_FILE_PERMISSIONS_MODE = 0o644
STATIC_DIRECTORY_PERMISSIONS_MODE = 0o755

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        # NOT decoration — without these, /static/ is a 403 in production.
        #
        # StaticFilesStorage is a FileSystemStorage, so when it is constructed
        # without explicit modes it falls back to FILE_UPLOAD_PERMISSIONS /
        # FILE_UPLOAD_DIRECTORY_PERMISSIONS below. Those are 0600/0700 because
        # they were chosen for uploaded candidate documents, and that is right
        # for documents and wrong for the admin's stylesheets: collectstatic
        # then writes STATIC_ROOT as uid 10001, mode 0600, and nginx — a
        # different uid (101) in a different container, reading the shared
        # `static_data` volume — cannot open a single file. Measured on the VPS
        # 2026-08-01: /app/staticfiles/admin was drwx------ and every asset
        # -rw-------.
        #
        # STORAGES[...]["OPTIONS"] is passed straight to the storage class as
        # keyword arguments (StorageHandler.create_storage), and
        # FileSystemStorage.__init__ takes exactly these two names.
        "OPTIONS": {
            "file_permissions_mode": STATIC_FILE_PERMISSIONS_MODE,
            "directory_permissions_mode": STATIC_DIRECTORY_PERMISSIONS_MODE,
        },
    },
}

# Uploaded candidate documents are personal data. Keep them off the world-readable
# default and never serve MEDIA_ROOT directly from nginx.
#
# These are the GLOBAL FileSystemStorage fallback, which is why STORAGES above
# has to override them for staticfiles: STATIC_ROOT holds nothing but Django's
# own admin/DRF assets and is read by a second container.
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
# django-import-export
# ---------------------------------------------------------------------------
# Note what is NOT here: `IMPORT_EXPORT_FORMATS`. That setting has to hold
# format *classes*, not dotted paths — `BaseExportMixin.get_export_formats`
# calls `f()` on every entry — so honouring it from this module would mean
# importing `import_export.formats.base_formats` (and through it tablib) while
# settings are still being read. Every other third-party name in this file is a
# string for exactly that reason. The allowed formats are pinned on
# `apps.common.admin_integrations.EvidenceExportAdminMixin` instead, which is
# imported from `admin.py` — i.e. after the app registry is ready.

# CSV injection. An exported cell whose text begins with `=`, `+`, `-` or `@`
# is a *formula* to Excel and LibreOffice, not a string. These exports carry
# `user_agent` and `payload` — attacker-influenced free text — and their
# intended reader is somebody opening the file in a spreadsheet, which is
# precisely the vulnerable path. The library prefixes such values with a
# tab when this is on. Off is the library's default and is wrong for this data.
IMPORT_EXPORT_ESCAPE_FORMULAE_ON_EXPORT = True

# Note what is also NOT here: `IMPORT_EXPORT_SKIP_ADMIN_LOG`. It reads as the
# setting that controls "is an export recorded", and it is not — it governs
# **imports** only, and nothing in the library writes any log line on export.
# Setting it would be a comment that looks like a control. "Who took a copy of
# the evidence" is recorded to `tenancy.AuditLog` by
# `apps.common.admin_integrations.EvidenceExportAdminMixin` instead.


# ---------------------------------------------------------------------------
# django-object-actions
# ---------------------------------------------------------------------------
# Object-action buttons submit a POST form, not a GET link. This is a
# correctness setting, not a style one, and the library's own default is the
# wrong way round (it emits a DeprecationWarning saying so and plans to flip in
# a future release).
#
# With "GET" the button renders as a bare `<a href>`: no CSRF token, and a
# state-changing URL reachable by anything that follows links — a link
# prefetcher, a browser's address-bar prediction, an antivirus proxy scanning a
# pasted admin URL. The one action wired today
# (`accounts.SuppressedAddressAdmin.unsuppress`) *deletes a row*. With "POST"
# the template renders `<form method="post">{% csrf_token %}`, which is what a
# destructive admin control has to be.
#
# The action views accept both verbs regardless (`DEFAULT_METHODS_ALLOWED`);
# this decides what the button emits.
DJANGO_OBJECT_ACTIONS_DEFAULT_HTTP_METHOD = "POST"


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


# ---------------------------------------------------------------------------
# n8n — the automation layer (apps.automation, READ-ONLY)
# ---------------------------------------------------------------------------
# The instance is the PERSONAL n8n account (app.n8n-studio.com). `apps.automation`
# only ever issues GET /api/v1/workflows and GET /api/v1/executions against it —
# there is no write path in this codebase.
#
# The canonical names are N8N_URL / N8N_API_TOKEN. The N8N_PERSONAL_* fallbacks
# exist because the repository-root .env (which config/env.py already loads) has
# carried those two names since before the backend existed; spelling them here
# means a working developer checkout needs no .env edit, and production can set
# the canonical names without touching anything else.
#
# An EMPTY token is a supported state: the admin panel renders a "not configured"
# card and makes no request. It must never be a 500 and never a boot failure —
# n8n is observability, not a dependency.

N8N_URL = env.get("N8N_URL", "") or env.get("N8N_PERSONAL_URL", "")

#: Full-privilege n8n API key. Never logged (apps.common.logging.RedactingFilter
#: already matches `api-key: …`), never rendered, never put in a cache key in the
#: clear (apps.automation.client hashes it into the key instead).
N8N_API_TOKEN = env.get("N8N_API_TOKEN", "") or env.get(
    "N8N_PERSONAL_ACCESS_TOKEN", ""
)

#: Hard wall-clock budget for ONE n8n request, seconds. Bounds connect and read.
N8N_TIMEOUT = env.get_int("N8N_TIMEOUT", 5)

#: How long a successful response is reused. Keeps repeated admin loads off the
#: n8n API entirely.
N8N_CACHE_TTL = env.get_int("N8N_CACHE_TTL", 60)

#: Failures are cached too, for much less time. Without this, a dead n8n costs
#: every single admin page load a full N8N_TIMEOUT stall.
N8N_CACHE_ERROR_TTL = env.get_int("N8N_CACHE_ERROR_TTL", 15)

#: Workflows requested in one page (the instance currently has 39).
N8N_WORKFLOW_LIMIT = env.get_int("N8N_WORKFLOW_LIMIT", 100)

#: Recent executions listed per workflow.
N8N_EXECUTION_LIMIT = env.get_int("N8N_EXECUTION_LIMIT", 5)

#: How many workflows get an executions lookup at all — one HTTP round trip
#: each, and only findajob's own workflows are ever candidates.
N8N_EXECUTION_WORKFLOW_LIMIT = env.get_int("N8N_EXECUTION_WORKFLOW_LIMIT", 8)

#: Wall-clock ceiling for the whole execution-fetch loop, seconds. Reached =>
#: the remaining workflows render without executions instead of hanging.
N8N_PANEL_BUDGET = env.get_int("N8N_PANEL_BUDGET", 12)
