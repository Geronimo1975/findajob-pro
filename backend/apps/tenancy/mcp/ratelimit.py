"""Per-token request budget for ``/mcp/`` (spec §4).

Stub so the view imports; the real sliding-window budget lands in Task 4.
"""
from __future__ import annotations


class McpRateLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__("rate limited")
        self.retry_after = max(1, int(retry_after))


def check_budget(token_id: int) -> None:
    return None
