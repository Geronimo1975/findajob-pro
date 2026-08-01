"""Shared helpers for the ``apps.accounts`` test modules.

Deliberately depends on *app* APIs (``apps.legal.models``, ``apps.legal.hashing``)
rather than on ``tests.legal.support``: a helper in another test package is not
a contract, and coupling the accounts suite to it would make a rename over there
break the login tests for no reason.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone as dt_timezone

from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase

from apps.legal.hashing import compute_content_hash
from apps.legal.models import LegalDocument

#: The plaintext code sits on its own line in the plain-text body.
CODE_RE = re.compile(r"^\s*(\d{6})\s*$", re.MULTILINE)

#: What ``seed_legal_documents`` writes. ``apps.legal`` derives versions from the
#: document date, so any stable string works for a test.
TERMS_VERSION = "2026-08-01"

REQUEST_URL = "/api/v1/auth/request-code/"
VERIFY_URL = "/api/v1/auth/verify-code/"
LOGOUT_URL = "/api/v1/auth/logout/"
CSRF_URL = "/api/v1/auth/csrf/"
ME_URL = "/api/v1/me/"


def seed_legal_documents(version: str = TERMS_VERSION) -> None:
    """Minimal binding (German) ``terms`` + ``privacy`` rows.

    ``apps.legal.services.record_registration_consent`` fails **closed** with a
    503 when these are missing, so every test that registers a *new* user needs
    them. That coupling is real and deliberate: an unseeded deployment must not
    be able to create accounts with no consent record.
    """
    for doc_id in ("terms", "privacy"):
        sections = [{"heading": "1", "body": f"{doc_id} body"}]
        LegalDocument.objects.create(
            doc_id=doc_id,
            lang="de",
            version=version,
            title=doc_id.title(),
            sections=sections,
            note="",
            content_hash=compute_content_hash(
                doc_id=doc_id,
                lang="de",
                version=version,
                title=doc_id.title(),
                sections=sections,
            ),
            effective_from=datetime(2026, 8, 1, tzinfo=dt_timezone.utc),
            is_current=True,
        )


def extract_code(message=None) -> str:
    """Pull the six digits out of a sent message, the way a user would."""
    message = message if message is not None else mail.outbox[-1]
    match = CODE_RE.search(message.body)
    assert match, f"No 6-digit code found in the email body:\n{message.body}"
    return match.group(1)


class AuthTestCase(TestCase):
    """Base class that isolates the rate-limit budgets.

    ``LocMemCache`` is process-wide and survives between tests, so without this
    the fourth test to request a code for the same address would hit the 60 s
    cooldown left behind by the first.
    """

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        mail.outbox.clear()

    def tearDown(self) -> None:
        cache.clear()
        super().tearDown()

    # -- convenience wrappers -------------------------------------------

    def post_json(self, url: str, payload: dict, *, client: Client | None = None, **extra):
        return (client or self.client).post(
            url, payload, content_type="application/json", **extra
        )

    def request_code(self, email: str, lang: str = "en", **extra):
        return self.post_json(REQUEST_URL, {"email": email, "lang": lang}, **extra)

    def verify(self, email: str, code: str, **body):
        payload = {"email": email, "code": code}
        payload.update(body)
        return self.post_json(VERIFY_URL, payload)

    def request_and_read_code(self, email: str, lang: str = "en") -> str:
        response = self.request_code(email, lang)
        assert response.status_code == 202, response.content
        return extract_code()


def csrf_client() -> tuple[Client, str]:
    """A client with real CSRF enforcement plus a matching header token.

    The token comes from ``GET /api/v1/auth/csrf/``, i.e. the same call
    ``web/api.js``'s ``bootstrapCsrf()`` makes, so this exercises the real
    contract rather than a hand-minted pair.
    """
    client = Client(enforce_csrf_checks=True)
    response = client.get(CSRF_URL)
    assert response.status_code == 200, response.content
    return client, client.cookies["csrftoken"].value
