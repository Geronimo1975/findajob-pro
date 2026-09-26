"""Log redaction (``apps.common.logging``)."""
from __future__ import annotations


def test_mcp_token_is_redacted():
    from apps.common.logging import REDACTED, redact
    raw = "fjp_" + "A" * 40
    assert raw not in redact(f"token seen {raw} in body")
    assert REDACTED in redact(f"token seen {raw} in body")
