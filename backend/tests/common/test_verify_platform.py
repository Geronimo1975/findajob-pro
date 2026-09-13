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

    def test_anchor_and_jsonld_links_are_not_origins(self):
        from apps.common.verify.frontend import third_party_origins

        html = ('<script type="application/ld+json">{"sameAs": ["https://georgeai.dev/"]}</script>'
                '<p>Built by <a href="https://georgeai.dev/">George</a>.</p>'
                '<link rel="stylesheet" href="https://fonts.example.com/a.css">')
        assert third_party_origins(html, "https://findajob.pro") == {"https://fonts.example.com"}

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


class TestDeliverability:
    def test_dig_txt_parses_and_strips_quotes(self):
        from apps.common.verify.deliverability import dig_txt

        with mock.patch("subprocess.run") as sp:
            sp.return_value = mock.Mock(returncode=0, stdout='"v=spf1 include:spf.brevo.com ~all"\n')
            assert dig_txt("findajob.pro") == ["v=spf1 include:spf.brevo.com ~all"]

    def test_inbox_not_spam(self):
        from apps.common.verify.deliverability import run as deliverability_run

        raw = ("Authentication-Results: mx.google.com; spf=pass; dkim=pass; dmarc=pass\n"
               "Subject: 123456 is your findajob.pro login code\n\nbody\n")
        ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
        ctx.otp_message = email_lib.message_from_string(raw)
        ctx.otp_folder = "INBOX"
        with mock.patch("apps.common.verify.deliverability.dig_txt",
                        side_effect=lambda name: ["v=spf1 x"] if "dmarc" not in name else ["v=DMARC1; p=none"]):
            report = deliverability_run(ctx)
        by_name = {r.name: r.status for r in report.results}
        assert by_name["OTP landed in Inbox (not Spam)"] == "PASS"
        assert by_name["SPF/DKIM/DMARC all pass"] == "PASS"

    def test_spam_folder_fails(self):
        from apps.common.verify.deliverability import run as deliverability_run

        ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
        ctx.otp_message = email_lib.message_from_string("Subject: t\n\nb\n")
        ctx.otp_folder = "[Gmail]/Spam"
        with mock.patch("apps.common.verify.deliverability.dig_txt", return_value=[]):
            report = deliverability_run(ctx)
        assert report.failed is True

    def test_skips_without_otp_mail(self):
        from apps.common.verify.deliverability import run as deliverability_run

        ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
        with mock.patch("apps.common.verify.deliverability.dig_txt", return_value=[]):
            report = deliverability_run(ctx)
        assert any(r.status == "SKIP" and "auth-otp" in r.detail for r in report.results)


class TestOutbox:
    def _ctx(self, get_resp):
        session = mock.Mock()
        session.get.return_value = get_resp
        session.cookies.get.return_value = "tok"
        return VerifyContext(target="https://x", session=session, env={})

    def test_skip_when_not_deployed(self):
        from apps.common.verify import outbox as outbox_mod

        resp = mock.Mock(status_code=404)
        resp.json.return_value = {"detail": "Not found."}  # Django HTML 404 / non-API shape
        resp.json.side_effect = ValueError
        report = outbox_mod.run(self._ctx(resp))
        assert any(r.status == "SKIP" and "not deployed" in r.detail for r in report.results)

    def test_skip_when_not_deployed_canonical_404(self):
        """Any canonical-shaped 404 other than mailbox_not_connected is also
        "not deployed" — handler404 (apps/common/errors.py::api_page_not_found)
        wraps every unmatched /api/ route in the same error shape as a real
        view's 404, so an unmounted apps.tenancy.outbox.urls looks identical to a
        genuinely-missing sub-path.
        """
        from apps.common.verify import outbox as outbox_mod

        resp = mock.Mock(status_code=404)
        resp.json.return_value = {"error": {"code": "not_found", "detail": "Not found."}}
        report = outbox_mod.run(self._ctx(resp))
        assert report.failed is False
        assert any(r.status == "SKIP" and "not deployed" in r.detail for r in report.results)

    def test_skip_when_no_mailbox_connected(self):
        from apps.common.verify import outbox as outbox_mod

        resp = mock.Mock(status_code=404)
        resp.json.return_value = {"error": {"code": "mailbox_not_connected", "detail": "x"}}
        report = outbox_mod.run(self._ctx(resp))
        assert report.failed is False
        assert any("connect a mailbox" in r.detail for r in report.results)

    def test_non_json_200_body_does_not_crash(self):
        from apps.common.verify import outbox as outbox_mod

        resp = mock.Mock(status_code=200)
        resp.json.side_effect = ValueError
        report = outbox_mod.run(self._ctx(resp))
        assert isinstance(report, ModuleReport)
        assert all(r.name != "module crashed" for r in report.results)

    def test_secrets_never_in_payload(self):
        from apps.common.verify import outbox as outbox_mod

        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"provider": "smtp", "email_address": "a@b.c",
                                  "status": "connected", "password": "leak!"}
        report = outbox_mod.run(self._ctx(resp))
        assert any(r.status == "FAIL" and "secret" in r.name for r in report.results)

    def test_skip_inbox_check_when_mailbox_differs_from_verify_account(self):
        from apps.common.verify import outbox as outbox_mod

        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"provider": "smtp", "email_address": "other@elsewhere.com",
                                  "status": "connected"}
        session = mock.Mock()
        session.get.return_value = resp
        session.cookies.get.return_value = "tok"
        ctx = VerifyContext(
            target="https://x", session=session,
            env={"VERIFY_GMAIL_USER": "george@rotocon.world",
                 "VERIFY_GMAIL_APP_PASSWORD": "app-pw"},
        )
        report = outbox_mod.run(ctx)
        assert session.post.call_args_list == []
        assert any(r.status == "SKIP" and "differs" in r.detail for r in report.results)


class TestAutomation:
    def test_gated_is_pass(self):
        from apps.common.verify import automation as automation_mod

        # anonymous hit: Traefik basicAuth 401, or Django admin login redirect
        resp = mock.Mock(status_code=401, url="https://x/admin/automation/")
        with mock.patch("apps.common.verify.automation.requests.get", return_value=resp):
            ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
            report = automation_mod.run(ctx)
        assert report.failed is False
        assert any(r.status == "PASS" and "gated" in r.name for r in report.results)

    def test_open_panel_is_fail(self):
        from apps.common.verify import automation as automation_mod

        resp = mock.Mock(status_code=200, url="https://x/admin/automation/",
                         text="<h1>n8n panel</h1>")
        with mock.patch("apps.common.verify.automation.requests.get", return_value=resp):
            ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
            assert automation_mod.run(ctx).failed is True

    def test_not_deployed_is_skip(self):
        from apps.common.verify import automation as automation_mod

        resp = mock.Mock(status_code=404, url="https://x/admin/automation/")
        with mock.patch("apps.common.verify.automation.requests.get", return_value=resp):
            ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
            report = automation_mod.run(ctx)
        assert report.failed is False
        assert any(r.status == "SKIP" and "not deployed" in r.detail for r in report.results)


class TestN8n:
    def test_skips_without_creds(self):
        from apps.common.verify import n8n as n8n_mod

        ctx = VerifyContext(target="https://x", session=mock.Mock(), env={})
        report = n8n_mod.run(ctx)
        assert report.failed is False
        assert all(r.status == "SKIP" for r in report.results)

    def test_inactive_workflow_fails(self):
        from apps.common.verify import n8n as n8n_mod

        session = mock.Mock()
        wf_resp = mock.Mock(status_code=200)
        wf_resp.json.return_value = {"data": [
            {"id": "1", "name": "findajob scrape", "active": False}]}
        exec_resp = mock.Mock(status_code=200)
        exec_resp.json.return_value = {"data": []}
        with mock.patch("apps.common.verify.n8n.requests.get",
                        side_effect=[wf_resp, exec_resp]):
            ctx = VerifyContext(target="https://x", session=session,
                                env={"N8N_VERIFY_URL": "https://n8n.example",
                                     "N8N_VERIFY_API_KEY": "k"})
            report = n8n_mod.run(ctx)
        assert report.failed is True

    def test_success_execution_passes(self):
        from apps.common.verify import n8n as n8n_mod

        session = mock.Mock()
        wf_resp = mock.Mock(status_code=200)
        wf_resp.json.return_value = {"data": [
            {"id": "1", "name": "findajob scrape", "active": True}]}
        exec_resp = mock.Mock(status_code=200)
        exec_resp.json.return_value = {"data": [{"status": "success"}]}
        with mock.patch("apps.common.verify.n8n.requests.get",
                        side_effect=[wf_resp, exec_resp]):
            ctx = VerifyContext(target="https://x", session=session,
                                env={"N8N_VERIFY_URL": "https://n8n.example",
                                     "N8N_VERIFY_API_KEY": "k"})
            report = n8n_mod.run(ctx)
        assert report.failed is False
        by_name = {r.name: r.status for r in report.results}
        assert by_name["workflow 'findajob scrape' last execution"] == "PASS"

    def test_crashed_execution_fails(self):
        from apps.common.verify import n8n as n8n_mod

        session = mock.Mock()
        wf_resp = mock.Mock(status_code=200)
        wf_resp.json.return_value = {"data": [
            {"id": "1", "name": "findajob scrape", "active": True}]}
        exec_resp = mock.Mock(status_code=200)
        exec_resp.json.return_value = {"data": [{"status": "crashed"}]}
        with mock.patch("apps.common.verify.n8n.requests.get",
                        side_effect=[wf_resp, exec_resp]):
            ctx = VerifyContext(target="https://x", session=session,
                                env={"N8N_VERIFY_URL": "https://n8n.example",
                                     "N8N_VERIFY_API_KEY": "k"})
            report = n8n_mod.run(ctx)
        assert report.failed is True

    def test_unknown_status_skips(self):
        from apps.common.verify import n8n as n8n_mod

        session = mock.Mock()
        wf_resp = mock.Mock(status_code=200)
        wf_resp.json.return_value = {"data": [
            {"id": "1", "name": "findajob scrape", "active": True}]}
        exec_resp = mock.Mock(status_code=200)
        exec_resp.json.return_value = {"data": [{"status": "waiting"}]}
        with mock.patch("apps.common.verify.n8n.requests.get",
                        side_effect=[wf_resp, exec_resp]):
            ctx = VerifyContext(target="https://x", session=session,
                                env={"N8N_VERIFY_URL": "https://n8n.example",
                                     "N8N_VERIFY_API_KEY": "k"})
            report = n8n_mod.run(ctx)
        assert report.failed is False
        by_name = {r.name: r.status for r in report.results}
        assert by_name["workflow 'findajob scrape' last execution"] == "SKIP"
