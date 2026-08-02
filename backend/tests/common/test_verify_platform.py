"""Unit tests for the verify_platform framework. No live network."""
from __future__ import annotations

import json
from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command

from apps.common.verify.base import (
    CheckResult, ModuleReport, VerifyContext, failed, passed, skipped,
)


class TestReportAggregation:
    def test_module_report_failed_iff_any_fail(self):
        ok = ModuleReport("m", [passed("a"), skipped("b", "no creds")])
        bad = ModuleReport("m", [passed("a"), failed("c", "boom")])
        assert ok.failed is False
        assert bad.failed is True

    def test_skip_never_fails(self):
        rep = ModuleReport("m", [skipped("a"), skipped("b")])
        assert rep.failed is False


class TestCommand:
    def _run(self, modules, *args):
        out = StringIO()
        with mock.patch("apps.common.management.commands.verify_platform.MODULES", modules):
            call_command("verify_platform", *args, stdout=out)
        return out.getvalue()

    def test_exit_zero_when_all_pass(self):
        modules = {"m1": lambda ctx: ModuleReport("m1", [passed("ok")])}
        out = self._run(modules)
        assert "PASS" in out and "m1" in out

    def test_exit_one_on_any_fail(self):
        modules = {"m1": lambda ctx: ModuleReport("m1", [failed("x", "boom")])}
        with pytest.raises(SystemExit) as exc:
            self._run(modules)
        assert exc.value.code == 1

    def test_module_filter(self):
        seen = []
        modules = {
            "m1": lambda ctx: (seen.append("m1"), ModuleReport("m1", [passed("a")]))[1],
            "m2": lambda ctx: (seen.append("m2"), ModuleReport("m2", [passed("a")]))[1],
        }
        self._run(modules, "--module", "m2")
        assert seen == ["m2"]

    def test_json_output(self):
        modules = {"m1": lambda ctx: ModuleReport("m1", [skipped("a", "hint")])}
        out = self._run(modules, "--json")
        data = json.loads(out)
        assert data["modules"][0] == {
            "module": "m1",
            "results": [{"name": "a", "status": "SKIP", "detail": "hint"}],
        }

    def test_module_exception_becomes_fail(self):
        def blow_up(ctx):
            raise RuntimeError("connection refused")
        modules = {"m1": blow_up}
        with pytest.raises(SystemExit):
            out = StringIO()
            with mock.patch(
                "apps.common.management.commands.verify_platform.MODULES", modules
            ):
                call_command("verify_platform", stdout=out)


class TestContext:
    def test_api_joins(self):
        ctx = VerifyContext(target="https://findajob.pro", session=mock.Mock(), env={})
        assert ctx.api("/me/") == "https://findajob.pro/api/v1/me/"


class TestFrontendHelpers:
    def test_third_party_origin_detected(self):
        from apps.common.verify.frontend import third_party_origins

        html = '<script src="https://cdn.example.com/x.js"></script>'
        assert third_party_origins(html, "https://findajob.pro") == {"https://cdn.example.com"}

    def test_same_origin_and_relative_ok(self):
        from apps.common.verify.frontend import third_party_origins

        html = ('<script src="/vendor/react.js"></script>'
                '<link href="https://findajob.pro/fonts/a.woff2">')
        assert third_party_origins(html, "https://findajob.pro") == set()

    def test_resource_urls_parsed(self):
        from apps.common.verify.frontend import resource_urls

        html = ('<script>window.__resources = {"react": "/vendor/react.production.js",'
                ' "font": "/fonts/inter.woff2"};</script>')
        assert resource_urls(html) == ["/vendor/react.production.js", "/fonts/inter.woff2"]

    def test_resource_urls_absent(self):
        from apps.common.verify.frontend import resource_urls

        assert resource_urls("<html></html>") == []
