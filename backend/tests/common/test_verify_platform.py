"""Unit tests for the verify_platform framework. No live network."""
from __future__ import annotations

import email as email_lib
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

    def test_slash_less_resource_urls_joined_correctly(self):
        """Regression: slash-less __resources paths must join with domain correctly."""
        from apps.common.verify.frontend import run

        html = ('<script>window.__resources = {"react": "vendor/react.production.min.js"};</script>'
                '<meta name="csp" value="default-src \'self\'">')
        session = mock.Mock()
        session.get.side_effect = [
            mock.Mock(status_code=200, text=html, headers={"Content-Security-Policy": "default-src 'self'"}),
            mock.Mock(status_code=200),
        ]
        ctx = VerifyContext(target="https://findajob.pro", session=session, env={})
        report = run(ctx)

        # Verify the resource URL was joined correctly (without double-slash issue)
        calls = [c for c in session.get.call_args_list if "vendor" in str(c)]
        assert len(calls) == 1
        assert calls[0][0][0] == "https://findajob.pro/vendor/react.production.min.js"


class TestErrorShape:
    def test_canonical_shape(self):
        from apps.common.verify.api_gate import is_error_shape

        assert is_error_shape({"error": {"code": "not_authenticated", "detail": "x"}})

    def test_rejects_drf_default(self):
        from apps.common.verify.api_gate import is_error_shape

        assert not is_error_shape({"detail": "Not found."})
        assert not is_error_shape({"error": "nope"})
        assert not is_error_shape(None)


class TestOtpExtraction:
    def test_extracts_leading_six_digits(self):
        from apps.common.verify.gmail import extract_otp

        assert extract_otp("482913 is your findajob.pro login code") == "482913"

    def test_german_subject(self):
        from apps.common.verify.gmail import extract_otp

        assert extract_otp("482913 ist Ihr Anmeldecode für findajob.pro") == "482913"

    def test_no_code(self):
        from apps.common.verify.gmail import extract_otp

        assert extract_otp("Welcome to findajob.pro") is None


class TestAuthResults:
    def test_parses_pass_verdicts(self):
        from apps.common.verify.gmail import auth_results

        raw = (
            "Authentication-Results: mx.google.com;\n"
            " spf=pass (google.com: domain of x designates y) smtp.mailfrom=x;\n"
            " dkim=pass header.i=@mail.findajob.pro;\n"
            " dmarc=pass (p=NONE) header.from=findajob.pro\n"
            "From: findajob.pro <noreply@findajob.pro>\n"
            "Subject: t\n\nbody\n"
        )
        msg = email_lib.message_from_string(raw)
        assert auth_results(msg) == {"spf": "pass", "dkim": "pass", "dmarc": "pass"}

    def test_missing_header(self):
        from apps.common.verify.gmail import auth_results

        msg = email_lib.message_from_string("Subject: t\n\nbody\n")
        assert auth_results(msg) == {}


class TestGmailClient:
    def test_connect_passes_timeout(self):
        from apps.common.verify.gmail import GmailClient, CONNECT_TIMEOUT

        with mock.patch("imaplib.IMAP4_SSL") as mock_imap:
            mock_conn = mock.Mock()
            mock_imap.return_value = mock_conn
            client = GmailClient("user@example.com", "app_password")
            client._connect()
            # Verify IMAP4_SSL was called with the timeout kwarg
            mock_imap.assert_called_once_with("imap.gmail.com", timeout=CONNECT_TIMEOUT)

    def test_search_escapes_quotes_in_subject(self):
        from apps.common.verify.gmail import GmailClient
        from datetime import datetime

        mock_conn = mock.Mock()
        mock_conn.select.return_value = ("OK", None)
        mock_conn.search.return_value = ("OK", [b"1"])
        mock_conn.fetch.return_value = ("OK", [(None, b"test message")])

        with mock.patch("email.message_from_bytes") as mock_from_bytes:
            mock_from_bytes.return_value = mock.Mock()
            client = GmailClient("user@example.com", "app_password")
            client._search(mock_conn, "INBOX", 'test"subject', datetime(2026, 8, 3))
            # Verify the subject was escaped in the search call
            call_args = mock_conn.search.call_args
            # The last argument should contain the escaped quote
            assert '"test\\"subject"' in call_args[0]

    def test_search_escapes_backslashes_in_subject(self):
        from apps.common.verify.gmail import GmailClient
        from datetime import datetime

        mock_conn = mock.Mock()
        mock_conn.select.return_value = ("OK", None)
        mock_conn.search.return_value = ("OK", [b"1"])
        mock_conn.fetch.return_value = ("OK", [(None, b"test message")])

        with mock.patch("email.message_from_bytes") as mock_from_bytes:
            mock_from_bytes.return_value = mock.Mock()
            client = GmailClient("user@example.com", "app_password")
            client._search(mock_conn, "INBOX", 'test\\subject', datetime(2026, 8, 3))
            # Verify the subject was escaped in the search call
            call_args = mock_conn.search.call_args
            # The last argument should contain the escaped backslash
            assert '"test\\\\subject"' in call_args[0]


class TestCsrfHeaders:
    def test_reads_token_from_cookiejar(self):
        from apps.common.verify.auth_otp import csrf_headers

        session = mock.Mock()
        session.cookies.get.return_value = "tok123"
        h = csrf_headers(session, "https://findajob.pro")
        assert h["X-CSRFToken"] == "tok123"
        assert h["Referer"] == "https://findajob.pro/"


class TestAuthOtpSkips:
    def test_skips_without_gmail_creds(self):
        """request-code 202 still runs; code fetch SKIPs, nothing FAILs."""
        session = mock.Mock()
        resp202 = mock.Mock(status_code=202)
        respcsrf = mock.Mock(status_code=200)
        session.get.return_value = respcsrf
        session.post.return_value = resp202
        session.cookies.get.return_value = "tok"
        session.cookies.keys.return_value = ["csrftoken"]
        from apps.common.verify import auth_otp
        ctx = VerifyContext(target="https://x", session=session, env={})
        report = auth_otp.run(ctx)
        assert report.failed is False
        assert any(r.status == "SKIP" for r in report.results)
