"""CI settings — identical to ``test``, but against a real PostgreSQL 16.

Used by ONE job (``migrations-pg16``) and nothing else. The test suite itself
stays on ``config.settings.test`` (SQLite ``:memory:`` + LocMemCache + locmem
email), because a 4-second suite that needs no infrastructure is the reason CI
gives useful feedback at all — adding a database service to it would triple the
job's wall-clock and buy nothing, since the models are exercised identically.

What this module is for is the narrow class of bug SQLite cannot see: a
migration that applies cleanly on SQLite and fails on PostgreSQL 16. SQLite
rebuilds a table for most schema changes and is permissive about identifier
length, constraint timing and type coercion; Postgres is not.

Be honest about the ceiling, because it is the difference between a check and
a false sense of one: this migrates a database that starts EMPTY. It catches
"the migration is invalid on PG16" and nothing else. The migrations that hurt
in production are data-dependent — a unique index over rows that already hold
duplicates, a NOT NULL backfill that rewrites a populated table, an
``AddConstraint`` that takes ACCESS EXCLUSIVE while requests are in flight.
None of those fail on an empty database. Closing that gap would mean restoring
a production dump into CI, which puts GDPR-scoped personal data on a
third-party runner; that trade is deliberately not made here.
"""

from __future__ import annotations

from config import env
from config.settings.test import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.get("POSTGRES_DB", "findajob_ci"),
        "USER": env.get("POSTGRES_USER", "findajob"),
        "PASSWORD": env.get("POSTGRES_PASSWORD", ""),
        "HOST": env.get("POSTGRES_HOST", "127.0.0.1"),
        "PORT": env.get("POSTGRES_PORT", "5432"),
        "OPTIONS": {"connect_timeout": 5},
    }
}
