"""Per-token budget for ``tools/call`` and friends (spec §7).

Fixed windows, two of them. **Fails closed**: if the cache cannot answer, the
assistant is told to retry later while the browser is unaffected — the same
policy as the OTP verify budget, and for the same reason: an unknown budget
must not become an unlimited one.
"""
from __future__ import annotations

import logging
import time

from django.core.cache import cache

logger = logging.getLogger("findajob.mcp")

PER_MINUTE = 60
PER_HOUR = 600
_WINDOWS = (("m", 60, PER_MINUTE), ("h", 3600, PER_HOUR))


class McpRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = max(1, int(retry_after))


def _key(token_id: int, suffix: str) -> str:
    return f"mcp:rl:{token_id}:{suffix}"


def _hit(key: str, window: int) -> int:
    """Increment ``key`` inside a fixed ``window``; return the new count."""
    if cache.add(key, 0, window):
        cache.set(key + ":exp", time.time() + window, window)
    try:
        return int(cache.incr(key))
    except ValueError:
        # Expired between add() and incr(): restart the window.
        cache.set(key, 1, window)
        cache.set(key + ":exp", time.time() + window, window)
        return 1


def _retry_after(key: str, window: int) -> int:
    deadline = cache.get(key + ":exp")
    if not deadline:
        return window
    remaining = int(float(deadline) - time.time())
    return remaining if remaining > 0 else window


def check_budget(token_id: int) -> None:
    for suffix, window, limit in _WINDOWS:
        key = _key(token_id, suffix)
        try:
            count = _hit(key, window)
        except Exception as exc:  # noqa: BLE001 - any cache failure closes the gate
            logger.warning("MCP rate-limit cache unavailable (%s): %s", suffix, exc)
            raise McpRateLimited(window) from exc
        if count > limit:
            raise McpRateLimited(_retry_after(key, window))
