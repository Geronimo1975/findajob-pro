"""Module registry — insertion order is execution order."""
from __future__ import annotations

from typing import Callable

from apps.common.verify.base import ModuleReport, VerifyContext

MODULES: dict[str, Callable[[VerifyContext], ModuleReport]] = {}
# Later tasks register: api-gate, auth-otp, mail-deliverability,
# outbox, automation, n8n — by importing their module and adding it here.

from apps.common.verify import api_gate, auth_otp, deliverability, frontend, outbox

MODULES["frontend"] = frontend.run
MODULES["api-gate"] = api_gate.run
MODULES["auth-otp"] = auth_otp.run
MODULES["mail-deliverability"] = deliverability.run
MODULES["outbox"] = outbox.run
# MODULES["logout"] = auth_otp.finish  — registered last, after Task 8
