"""Module registry — insertion order is execution order."""
from __future__ import annotations

from typing import Callable

from apps.common.verify.base import ModuleReport, VerifyContext

MODULES: dict[str, Callable[[VerifyContext], ModuleReport]] = {}

from apps.common.verify import (
    api_gate, auth_otp, automation, deliverability, frontend, mcp, n8n, outbox,
)

MODULES["frontend"] = frontend.run
MODULES["api-gate"] = api_gate.run
MODULES["auth-otp"] = auth_otp.run
MODULES["mail-deliverability"] = deliverability.run
MODULES["outbox"] = outbox.run
MODULES["automation"] = automation.run
MODULES["n8n"] = n8n.run
MODULES["mcp"] = mcp.run
MODULES["logout"] = auth_otp.finish  # must stay last — tears down the session
