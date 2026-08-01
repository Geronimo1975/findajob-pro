"""Tiny env reader built on ``python-dotenv``.

We deliberately do *not* add ``django-environ``: the approved dependency list is
Django, DRF, psycopg, django-anymail, python-dotenv, gunicorn. Everything the
settings modules need is 60 lines of parsing, so it lives here.

Load order (first file wins for a given key; later files never override):

1. ``backend/.env``               — the backend's own environment
2. ``<repo root>/.env``           — the pre-existing repo-level dev env

Real process environment always wins over both, which is what the VPS/Docker
deployment relies on.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

#: ``backend/`` — the directory that contains ``manage.py``.
BASE_DIR = Path(__file__).resolve().parent.parent

#: The repository root (``findajob/``), one level above ``backend/``.
REPO_ROOT = BASE_DIR.parent

_MISSING = object()

_TRUE = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE = frozenset({"0", "false", "no", "off", "n", "f", ""})


def load() -> None:
    """Populate ``os.environ`` from the dotenv files, without overriding."""
    for candidate in (BASE_DIR / ".env", REPO_ROOT / ".env"):
        if candidate.is_file():
            load_dotenv(candidate, override=False)


class ImproperlyConfiguredEnv(RuntimeError):
    """Raised when a required environment variable is missing or unparsable."""


def get(name: str, default=_MISSING) -> str:
    """Return ``name`` as a string. Raise when absent and no default is given."""
    value = os.environ.get(name, _MISSING)
    if value is _MISSING:
        if default is _MISSING:
            raise ImproperlyConfiguredEnv(
                f"Required environment variable {name!r} is not set."
            )
        return default
    return value


def get_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalised = raw.strip().lower()
    if normalised in _TRUE:
        return True
    if normalised in _FALSE:
        return False
    raise ImproperlyConfiguredEnv(
        f"Environment variable {name!r} must be a boolean, got {raw!r}."
    )


def get_int(name: str, default: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        if default is None:
            raise ImproperlyConfiguredEnv(
                f"Required integer environment variable {name!r} is not set."
            )
        return default
    try:
        return int(raw.strip())
    except ValueError as exc:  # pragma: no cover - configuration error path
        raise ImproperlyConfiguredEnv(
            f"Environment variable {name!r} must be an integer, got {raw!r}."
        ) from exc


def get_list(name: str, default: list[str] | None = None) -> list[str]:
    """Split a comma-separated variable, dropping empty entries."""
    raw = os.environ.get(name)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]
