"""Shared types for verify_platform modules (spec: docs/superpowers/specs/2026-08-02-verify-platform-design.md)."""
from __future__ import annotations

import email.message
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping

import requests

TIMEOUT = 15  #: per-request timeout, seconds (spec "Degradation rules")
OTP_WAIT = 90  #: max seconds to wait for the OTP mail


@dataclass
class CheckResult:
    name: str
    status: str  # "PASS" | "FAIL" | "SKIP"
    detail: str = ""


def passed(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name, "PASS", detail)


def failed(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name, "FAIL", detail)


def skipped(name: str, detail: str = "") -> CheckResult:
    return CheckResult(name, "SKIP", detail)


@dataclass
class ModuleReport:
    module: str
    results: list[CheckResult]

    @property
    def failed(self) -> bool:
        return any(r.status == "FAIL" for r in self.results)


@dataclass
class VerifyContext:
    """State shared across modules: one cookie-carrying session, cross-module mail."""

    target: str
    session: requests.Session
    env: Mapping[str, str]
    otp_message: email.message.Message | None = None
    otp_folder: str | None = None  # "INBOX" or "[Gmail]/Spam"
    otp_requested_at: datetime | None = None

    def api(self, path: str) -> str:
        return f"{self.target}/api/v1{path}"
