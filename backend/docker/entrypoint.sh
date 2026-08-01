#!/bin/sh
# findajob.pro API container entrypoint.
#
# -----------------------------------------------------------------------------
# THIS SCRIPT DOES NOT RUN MIGRATIONS. THAT IS DELIBERATE.
# -----------------------------------------------------------------------------
# An entrypoint `manage.py migrate` runs once per container, i.e. once per
# replica. `docker compose up -d --scale api=3` then starts three processes that
# race the same schema write; Django's migration lock is advisory-per-connection
# on PostgreSQL, so the losers either block for the whole migration or die on a
# duplicate relation. It also silently couples "restart the API" to "mutate the
# production schema", which is exactly the operation that should require a human.
#
# Migrations are an explicit, separate step — see deploy/README.md §3:
#
#     docker compose run --rm -e DJANGO_COLLECTSTATIC=0 api python manage.py migrate
#
# -----------------------------------------------------------------------------
# What it DOES do: collectstatic, and therefore Django's system checks.
# -----------------------------------------------------------------------------
# `collectstatic` runs the ordinary (non-`--deploy`) system checks, which is
# where config.checks lives. That is what makes spec §6's "refuse to boot when
# the mail configuration cannot deliver" literally true: findajob.E001/E002/E003
# abort this script, `set -e` propagates, and the container exits non-zero
# instead of serving a green UI that mails nobody.
#
# It is also cheap and per-container-local (admin + DRF assets into the image's
# own /app/staticfiles), so unlike a migration it is safe to repeat per replica.
# Production uses ManifestStaticFilesStorage, so the manifest must exist before
# the first admin request or every {% static %} lookup raises.
#
# Set DJANGO_COLLECTSTATIC=0 to skip it — used by one-off `docker compose run`
# management commands, where it is pure overhead.

set -eu

if [ "${DJANGO_COLLECTSTATIC:-1}" = "1" ]; then
    echo "entrypoint: collectstatic (also runs Django system checks)" >&2
    python manage.py collectstatic --noinput
fi

# exec: gunicorn becomes PID 1 and receives SIGTERM directly from Docker, so
# `docker compose stop` performs a graceful worker drain instead of a 10-second
# wait followed by SIGKILL mid-request.
exec "$@"
