"""Gunicorn configuration for the findajob.pro API container.

Referenced by the image's CMD:

    gunicorn config.wsgi:application --config /app/gunicorn.conf.py

Every value below is overridable through the environment so the same image can
be tuned on the VPS without a rebuild. Nothing here talks to PostgreSQL or
Redis: gunicorn must be able to start even when a dependency is down, so the
failure shows up as 500s on the affected routes instead of a boot loop.
"""

from __future__ import annotations

import multiprocessing
import os


def _str(name: str, default: str) -> str:
    """Environment override, treating an EMPTY value as "not set".

    backend/.env.example lists these keys with empty values so the operator can
    see them; a plain ``os.environ.get(name, default)`` would then return ``""``
    and, for example, silently disable the access log.
    """
    raw = os.environ.get(name, "")
    return raw.strip() or default


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _default_workers() -> int:
    """2 * CPU + 1, clamped.

    Clamped low on purpose: every worker holds its own persistent PostgreSQL
    connections (``CONN_MAX_AGE=60``), and PostgreSQL 16 ships with
    ``max_connections=100``. The math that must stay true is

        workers * threads  <=  max_connections - headroom

    The clamp caps it at 6, so the worst case with the default 4 threads is
    6 * 4 = 24 connections — plenty of headroom for `manage.py` one-offs,
    migrations and `psql`. On a 2-vCPU VPS it resolves to 5 workers.
    """
    return max(2, min(6, multiprocessing.cpu_count() * 2 + 1))


# ---------------------------------------------------------------------------
# Socket
# ---------------------------------------------------------------------------
# The container publishes no ports; nginx (findajob-web) reaches this over the
# docker network, and Traefik never talks to it directly.
bind = _str("GUNICORN_BIND", "0.0.0.0:8000")
backlog = _int("GUNICORN_BACKLOG", 512)


# ---------------------------------------------------------------------------
# Worker processes
# ---------------------------------------------------------------------------
# gthread, not sync: the login path sends the OTP email *synchronously inside
# the request* (spec §1) with a 5 s SMTP/API budget. With pure sync workers a
# handful of concurrent logins would occupy every worker for seconds and stall
# unrelated routes. Threads give that wait somewhere to sit.
worker_class = _str("GUNICORN_WORKER_CLASS", "gthread")
workers = _int("GUNICORN_WORKERS", 0) or _default_workers()
threads = _int("GUNICORN_THREADS", 4)

# Heartbeat files on a tmpfs. On some hosts /tmp is a slow overlay and the
# arbiter mistakes a slow write for a hung worker and kills it.
worker_tmp_dir = _str("GUNICORN_WORKER_TMP_DIR", "/dev/shm")

# Not preloading: preload_app forks after the app is imported, which is fine for
# Django itself but makes any accidental import-time connection shared across
# workers. The memory saved is not worth that class of bug.
preload_app = False

# 30 s is comfortably above the 5 s email timeout and the 5 s database connect
# timeout, so a worker that hits this really is wedged.
timeout = _int("GUNICORN_TIMEOUT", 30)
graceful_timeout = _int("GUNICORN_GRACEFUL_TIMEOUT", 30)

# nginx keeps upstream connections alive; a keepalive shorter than nginx's would
# race and produce spurious 502s.
keepalive = _int("GUNICORN_KEEPALIVE", 15)

# Recycle workers to bound the effect of any slow leak. The jitter stops all
# workers from retiring on the same request count.
max_requests = _int("GUNICORN_MAX_REQUESTS", 1000)
max_requests_jitter = _int("GUNICORN_MAX_REQUESTS_JITTER", 100)


# ---------------------------------------------------------------------------
# Request limits — a cheap first line against header/URL abuse.
# ---------------------------------------------------------------------------
limit_request_line = _int("GUNICORN_LIMIT_REQUEST_LINE", 4094)
limit_request_fields = _int("GUNICORN_LIMIT_REQUEST_FIELDS", 100)
limit_request_field_size = _int("GUNICORN_LIMIT_REQUEST_FIELD_SIZE", 8190)


# ---------------------------------------------------------------------------
# Proxy awareness
# ---------------------------------------------------------------------------
# NOTE: `forwarded_allow_ips` only controls whether *gunicorn* rewrites
# `wsgi.url_scheme` from X-Forwarded-Proto. Django does not use that: it reads
# the raw header itself via SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO',
# 'https') (config/settings/prod.py). So the default 127.0.0.1 is correct and
# deliberately left alone — widening it would add trust without adding function.
forwarded_allow_ips = _str("GUNICORN_FORWARDED_ALLOW_IPS", "127.0.0.1")


# ---------------------------------------------------------------------------
# Logging — stdout/stderr only, collected by `docker compose logs`.
# ---------------------------------------------------------------------------
accesslog = _str("GUNICORN_ACCESS_LOG", "-")
errorlog = _str("GUNICORN_ERROR_LOG", "-")
loglevel = _str("GUNICORN_LOG_LEVEL", "info")
capture_output = True

# Deliberately NOT the default format. The default logs `%(r)s`, the whole
# request line *including the query string*; this one logs the method and the
# path separately and never the query, so a code, token or e-mail address that
# ever ends up in a URL cannot be written to the log.
#
# `%({x-forwarded-for}i)s` is the client chain as nginx forwarded it; `%(h)s`
# would just be nginx's own container IP and is useless for abuse forensics.
# That does mean visitor IPs are written to the container log — the json-file
# driver in deploy/docker-compose.yml caps them at 3 x 10 MB, which is the
# de-facto retention window. See deploy/README.md §7.
access_log_format = _str(
    "GUNICORN_ACCESS_LOG_FORMAT",
    '%({x-forwarded-for}i)s "%(m)s %(U)s" %(s)s %(b)s %(L)s "%(a)s"',
)


def on_starting(server):  # pragma: no cover - process lifecycle hook
    server.log.info(
        "findajob api: %s workers x %s threads (%s), bind=%s",
        workers,
        threads,
        worker_class,
        bind,
    )
