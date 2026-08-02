"""``manage.py verify_platform`` — external smoke verification of findajob.pro.

Complements ``check_login_flow``: that command proves the SMTP leg without
mailing anyone; this one proves real end-to-end delivery to a real inbox and
sweeps every user-facing module. Spec:
docs/superpowers/specs/2026-08-02-verify-platform-design.md
"""
from __future__ import annotations

import json
import os
import sys

import requests
from django.core.management.base import BaseCommand, CommandError

from apps.common.verify import MODULES
from apps.common.verify.base import ModuleReport, VerifyContext, failed


class Command(BaseCommand):
    help = "Smoke-verify the live platform from the outside, module by module."

    def add_arguments(self, parser):
        parser.add_argument("--target", default="https://findajob.pro")
        parser.add_argument("--module", action="append", dest="modules")
        parser.add_argument("--json", action="store_true", dest="as_json")

    def handle(self, *args, **opts):
        selected = opts["modules"] or list(MODULES)
        unknown = [m for m in selected if m not in MODULES]
        if unknown:
            raise CommandError(f"unknown module(s): {', '.join(unknown)}")

        ctx = VerifyContext(
            target=opts["target"].rstrip("/"),
            session=requests.Session(),
            env=os.environ,
        )
        reports: list[ModuleReport] = []
        for name in selected:
            try:
                reports.append(MODULES[name](ctx))
            except Exception as exc:  # a crashed module is a FAIL, not a traceback
                reports.append(ModuleReport(name, [failed("module crashed", repr(exc))]))

        if opts["as_json"]:
            self.stdout.write(json.dumps({
                "target": ctx.target,
                "modules": [
                    {"module": r.module,
                     "results": [vars(c) for c in r.results]}
                    for r in reports
                ],
            }, indent=2))
        else:
            for r in reports:
                self.stdout.write(f"\n== {r.module} ==")
                for c in r.results:
                    line = f"  [{c.status}] {c.name}"
                    if c.detail:
                        line += f" — {c.detail}"
                    self.stdout.write(line)

        if any(r.failed for r in reports):
            sys.exit(1)
