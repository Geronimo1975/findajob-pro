"""Fernet encryption for mailbox credentials (outbox spec, "Data model").

The key comes from the ``OUTBOX_FERNET_KEY`` environment variable — a
urlsafe-base64 32-byte key, ``Fernet.generate_key()`` output. When unset
(dev/test) a key is derived from ``SECRET_KEY`` so local runs need no extra
setup; prod must set it explicitly so credential-key rotation is independent
of the Django secret.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet() -> Fernet:
    key = os.environ.get("OUTBOX_FERNET_KEY")
    if not key:
        digest = hashlib.sha256(settings.SECRET_KEY.encode()).digest()
        key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_credentials(credentials: dict[str, Any]) -> bytes:
    return _fernet().encrypt(json.dumps(credentials).encode())


def decrypt_credentials(blob: bytes) -> dict[str, Any]:
    """Raises :class:`cryptography.fernet.InvalidToken` on a wrong key."""
    return json.loads(_fernet().decrypt(bytes(blob)))


__all__ = ["encrypt_credentials", "decrypt_credentials", "InvalidToken"]
