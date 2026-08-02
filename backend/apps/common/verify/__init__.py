"""Module registry — insertion order is execution order."""
from __future__ import annotations

from typing import Callable

from apps.common.verify.base import ModuleReport, VerifyContext

MODULES: dict[str, Callable[[VerifyContext], ModuleReport]] = {}
# Later tasks register: api-gate, auth-otp, mail-deliverability,
# outbox, automation, n8n — by importing their module and adding it here.

from apps.common.verify import frontend

MODULES["frontend"] = frontend.run
