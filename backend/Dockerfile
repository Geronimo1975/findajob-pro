# syntax=docker/dockerfile:1.7
###############################################################################
# findajob.pro — API image (Django 6.0 + gunicorn).
#
# Built from the `backend/` directory as the build context:
#
#     build:
#       context: ./backend
#       dockerfile: Dockerfile
#
# Two stages. The builder resolves the pinned requirements into a virtualenv at
# /opt/venv; the runtime stage copies that venv and the source, and runs as an
# unprivileged user. No compiler, no apt packages and no pip cache survive into
# the runtime layer.
#
# WHAT THIS IMAGE DELIBERATELY DOES NOT DO
# ----------------------------------------
# It never runs `manage.py migrate`. See docker/entrypoint.sh and
# deploy/README.md §3: migrations are a separate, operator-driven step, because
# an entrypoint migration runs once per replica and turns a routine
# `docker compose up -d --scale api=N` into N concurrent schema writers.
###############################################################################

ARG PYTHON_VERSION=3.12-slim


# ---------------------------------------------------------------------------
# Stage 1 — builder: resolve dependencies into /opt/venv
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /build

# Every pin in requirements.txt (including psycopg[binary]) ships a manylinux
# wheel, so there is nothing to compile. Do NOT add build-essential / libpq-dev
# "just in case": it roughly doubles the builder image and hides the day a real
# source build sneaks into the dependency set.
COPY requirements.txt ./

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt \
 && /opt/venv/bin/pip check


# ---------------------------------------------------------------------------
# Stage 2 — runtime
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION} AS runtime

# PYTHONDONTWRITEBYTECODE: the source tree is owned by root and the process runs
#   as `findajob`, so .pyc writes would fail noisily anyway — and a stale .pyc
#   surviving a code change has cost this project real debugging time before.
# PYTHONUNBUFFERED: gunicorn logs to stdout and `docker compose logs` must show
#   a traceback the moment it happens, not when a 4 KB buffer fills.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    DJANGO_SETTINGS_MODULE=config.settings.prod \
    APP_HOME=/app

# Unprivileged, no shell, no home of its own beyond /app. Fixed uid/gid so a
# bind-mounted volume keeps stable ownership across image rebuilds.
RUN groupadd --system --gid 10001 findajob \
 && useradd --system --uid 10001 --gid findajob \
        --home-dir /app --shell /usr/sbin/nologin findajob

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Source is owned by root and only readable by the app user: the running process
# has no business rewriting its own code.
COPY --chown=root:root . /app

# Writable runtime directories, owned by the app user.
#  * staticfiles/ — collectstatic output (admin + DRF assets only; the product UI
#    is static files served by nginx straight from web/).
#  * media/ — uploaded candidate documents. 0700: personal data under GDPR, and
#    settings.base already sets FILE_UPLOAD_PERMISSIONS=0o600.
RUN mkdir -p /app/staticfiles /app/media \
 && chown findajob:findajob /app/staticfiles /app/media \
 && chmod 0755 /app/staticfiles \
 && chmod 0700 /app/media \
 && chmod 0755 /app/docker/entrypoint.sh \
 && rm -rf /app/db.sqlite3 /app/.env

USER findajob

EXPOSE 8000

# Liveness only — it must not depend on Postgres or Redis. A database blip has
# to surface as 500s on the affected routes, not as Docker restart-looping the
# whole API and taking the healthy routes down with it.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

ENTRYPOINT ["/app/docker/entrypoint.sh"]
# gunicorn.conf.py binds 0.0.0.0:8000, sizes the gthread pool and formats the
# access log so it never contains a query string. Every value in it is
# overridable through GUNICORN_* environment variables, so tuning the VPS never
# requires a rebuild. See backend/.env.example.
CMD ["gunicorn", "config.wsgi:application", "--config", "/app/gunicorn.conf.py"]
