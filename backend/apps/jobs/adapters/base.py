"""The adapter contract (architecture spec — Platform Adapter Pattern).

Every source platform implements exactly this surface:

- :attr:`BaseAdapter.slug` — must match a seeded ``Platform`` row;
- :meth:`BaseAdapter.fetch` — one HTTP round-trip to the official feed/API,
  returning the raw payload;
- :meth:`BaseAdapter.parse` — raw payload → list of :class:`NormalizedJob`.

Adapters never touch the ORM — persistence (upsert, dedup, limits) is
:func:`apps.jobs.services.ingest_platform`, identical for every platform.
Adapters exist only for LOW-risk official feeds/APIs; scraping LinkedIn or
Indeed is prohibited by the risk table, not merely unimplemented.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field

import requests

#: One conservative default for every adapter; per-platform pacing beyond a
#: single feed fetch is a send-time concern, not an ingest one.
TIMEOUT_SECONDS = 15

USER_AGENT = "findajob.pro ingest (+https://findajob.pro)"

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str) -> str:
    """Feed descriptions arrive as HTML; the UI renders plain text."""
    text = html.unescape(_TAG_RE.sub(" ", value or ""))
    return re.sub(r"[ \t\xa0]+", " ", text).strip()


def first_email(text: str) -> str | None:
    match = _EMAIL_RE.search(text or "")
    return match.group(0) if match else None


@dataclass
class NormalizedJob:
    """The normalized listing shape — mirrors ``JobListing`` field for field."""

    title: str
    company: str
    url: str
    location: str = ""
    salary: str = ""
    description: str = ""
    requirements: str = ""
    tags: list[str] = field(default_factory=list)
    platform_specific_data: dict = field(default_factory=dict)


class BaseAdapter:
    """Subclass, set :attr:`slug` and :attr:`feed_url`, implement :meth:`parse`."""

    slug: str
    feed_url: str

    def fetch(self):
        """One GET against the official feed. Raises ``requests.RequestException``
        on transport failure — the service layer reports it per platform and
        keeps ingesting the others."""
        response = requests.get(
            self.feed_url,
            timeout=TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()
        return response

    def parse(self, response) -> list[NormalizedJob]:  # pragma: no cover
        raise NotImplementedError

    def jobs(self) -> list[NormalizedJob]:
        return self.parse(self.fetch())
