"""``manage.py check_login_flow`` — the daily login healthcheck.

The point of this module is the *failure* tests. A healthcheck that has only
ever been observed passing is decoration: it tells you nothing, because nothing
has demonstrated it is capable of saying no. Every property the command claims to
check therefore has a test here that breaks that property and asserts a non-zero
exit.

How the command is driven
-------------------------

``Command.transport`` is the single seam between the command and the network.
:class:`FakeSite` replaces it with something better than a mock:

* ``/api/…`` is routed through :class:`django.test.Client` with
  ``enforce_csrf_checks=True``, so the **real** views, the **real** CSRF
  middleware and the **real** database run. Steps 2, 3, 6, 7, 8 and 9 are
  genuinely exercised end to end, not stubbed.
* the surfaces Django does not own — the static pages nginx serves and the
  ``/admin/`` gate Traefik answers — return canned replies, because in
  production they never reach Django at all.

``hook`` lets a single test bend one response (a redirect, a stray
``WWW-Authenticate``, a replayed code that succeeds) and assert the command
notices.
"""

from __future__ import annotations

import json
import smtplib
from contextlib import ExitStack
from io import StringIO
from unittest import mock

from django.contrib.admin.models import LogEntry
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings

from apps.accounts.models import EmailOTP, SuppressedAddress, User
from apps.common.management.commands import check_login_flow as flow
from apps.tenancy.models import Membership, Organization
from tests.accounts.support import seed_legal_documents

BASE = "http://testserver"

SMTP_SETTINGS = {
    "EMAIL_BACKEND": flow.SMTP_BACKEND,
    "EMAIL_HOST": "smtp-relay.example.invalid",
    "EMAIL_PORT": 587,
    "EMAIL_USE_TLS": True,
    "EMAIL_USE_SSL": False,
    "EMAIL_HOST_USER": "a4167f001@smtp.example.invalid",
    "EMAIL_HOST_PASSWORD": "an-smtp-key-that-is-never-printed",
    # Formatted on purpose: the envelope sender must come out as the bare
    # addr-spec, and a display name here is what proves the parsing works.
    "DEFAULT_FROM_EMAIL": '"findajob.pro" <codes@findajob.test>',
}


# ---------------------------------------------------------------------------
# A fake internet
# ---------------------------------------------------------------------------


class FakeSite:
    """Stands in for Traefik + nginx + Django, as seen from outside the box."""

    #: What the two non-Django surfaces answer in a healthy deployment.
    DEFAULT_STATIC = {
        "/": (200, [("Content-Type", "text/html")], b"<!doctype html><title>findajob</title>"),
        "/app.html": (200, [("Content-Type", "text/html")], b"<!doctype html><title>app</title>"),
        "/app.html?demo=1": (
            200, [("Content-Type", "text/html")], b"<!doctype html><title>app demo</title>",
        ),
        "/auth.html": (200, [("Content-Type", "text/html")], b"<!doctype html><title>auth</title>"),
        "/admin/": (
            401,
            [("WWW-Authenticate", 'Basic realm="findajob.pro admin"')],
            b"401 Unauthorized",
        ),
        "/adminx": (404, [("Content-Type", "text/html")], b"404"),
    }

    def __init__(self, *, static: dict | None = None, hook=None, explode=None) -> None:
        self.static = {**self.DEFAULT_STATIC, **(static or {})}
        self.hook = hook
        self.explode = explode
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, headers, body, timeout):
        path = url[len(BASE):] if url.startswith(BASE) else url
        self.calls.append((method, path))

        if self.explode is not None:
            raise self.explode

        if self.hook is not None:
            canned = self.hook(self, method, path, headers, body)
            if canned is not None:
                return canned

        if path.startswith("/api/"):
            return self.django(method, path, headers, body)

        status, response_headers, payload = self.static.get(
            path, (404, [("Content-Type", "text/html")], b"404")
        )
        return status, list(response_headers), payload

    # -- the real Django stack ------------------------------------------

    @staticmethod
    def django(method, path, headers, body):
        """Run the request through the real URLConf, views and middleware.

        A fresh ``Client`` per call, deliberately: the command owns the cookie
        jar and passes it in the ``Cookie`` header, and a client that also kept
        its own copy would quietly paper over a cookie the command failed to
        store.
        """
        extra = {}
        content_type = headers.get("Content-Type", "application/octet-stream")
        for name, value in headers.items():
            key = name.upper().replace("-", "_")
            if key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
                continue
            extra["HTTP_" + key] = value
        response = Client(enforce_csrf_checks=True).generic(
            method, path, data=body or b"", content_type=content_type, **extra
        )
        response_headers = list(response.items())
        for morsel in response.cookies.values():
            response_headers.append(("Set-Cookie", morsel.OutputString()))
        return response.status_code, response_headers, response.content


class FakeSMTP:
    """``smtplib.SMTP`` that records the conversation and transmits nothing."""

    instances: list["FakeSMTP"] = []

    def __init__(self, host=None, port=0, local_hostname=None, timeout=None, **kwargs):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.events: list[str] = []
        self.credentials: tuple[str, str] | None = None
        self._greeting = "smtp-relay.example.invalid ESMTP ready"
        self.envelope_sender = None
        self.envelope_recipient = None
        FakeSMTP.instances.append(self)

    def ehlo(self, name=""):
        self.events.append("ehlo")
        return 250, b"ok"

    def starttls(self, *args, **kwargs):
        self.events.append("starttls")
        return 220, b"ready to start TLS"

    def login(self, user, password, **kwargs):
        self.events.append("login")
        self.credentials = (user, password)
        return 235, b"authentication succeeded"

    # The envelope. Modelled because the command walks MAIL FROM / RCPT TO to
    # catch a sender the relay will not route for — AUTH alone cannot see that.
    # Test doubles that invent an API the real library lacks (or omit one it
    # has) let a check pass here and fail in production, which is the whole
    # failure mode this suite exists to prevent.
    mail_reply: tuple[int, bytes] = (250, b"sender ok")
    rcpt_reply: tuple[int, bytes] = (250, b"recipient ok")

    def mail(self, sender, options=()):
        self.events.append("mail")
        self.envelope_sender = sender
        return self.mail_reply

    def rcpt(self, recipient, options=()):
        self.events.append("rcpt")
        self.envelope_recipient = recipient
        return self.rcpt_reply

    def rset(self):
        self.events.append("rset")
        return 250, b"flushed"

    def sendmail(self, *args, **kwargs):  # pragma: no cover - must never happen
        raise AssertionError("check_login_flow must never send mail")

    def send_message(self, *args, **kwargs):  # pragma: no cover - must never happen
        raise AssertionError("check_login_flow must never send mail")

    def quit(self):
        self.events.append("quit")

    def close(self):
        self.events.append("close")


# ---------------------------------------------------------------------------
# Base case
# ---------------------------------------------------------------------------


class HealthcheckTestCase(TestCase):
    """Runs the command against a fake internet and returns the JSON summary."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        mail.outbox.clear()
        FakeSMTP.instances = []
        seed_legal_documents()

    def tearDown(self) -> None:
        cache.clear()
        super().tearDown()

    def run_check(self, site: FakeSite | None = None, *, smtp=FakeSMTP, **options):
        """Run the command. Returns ``(raised, summary, stdout, stderr)``.

        ``--json`` is always on so the assertions can read structured step
        results instead of scraping the human report; the human report goes to
        stderr, which is also asserted on at least once below.
        """
        site = site or FakeSite()
        out, err = StringIO(), StringIO()
        raised: CommandError | None = None
        with ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(flow.Command, "transport", staticmethod(site))
            )
            if smtp is not None:
                stack.enter_context(mock.patch.object(flow.smtplib, "SMTP", smtp))
            try:
                call_command(
                    "check_login_flow",
                    base=options.pop("base", BASE),
                    as_json=True,
                    stdout=out,
                    stderr=err,
                    **options,
                )
            except CommandError as exc:
                raised = exc
        payload = out.getvalue().strip()
        summary = json.loads(payload) if payload else {}
        return raised, summary, payload, err.getvalue()

    # -- helpers ---------------------------------------------------------

    def step(self, summary: dict, number: int) -> dict:
        for entry in summary["steps"]:
            if entry["n"] == number:
                return entry
        raise AssertionError(f"step {number} is missing from {summary['steps']}")

    def assertFailedWithStep(self, raised, summary, number: int):
        self.assertIsNotNone(raised, "the command exited 0 but should have failed")
        self.assertEqual(raised.returncode, 1)
        self.assertFalse(summary["ok"])
        entry = self.step(summary, number)
        self.assertFalse(entry["ok"], f"step {number} passed unexpectedly: {entry}")
        return entry

    def assertSentinelDatabaseIsEmpty(self):
        suffix = f"@{flow.SENTINEL_DOMAIN}"
        self.assertEqual(User.objects.filter(email__endswith=suffix).count(), 0)
        self.assertEqual(EmailOTP.objects.filter(email__endswith=suffix).count(), 0)
        self.assertEqual(
            SuppressedAddress.objects.filter(email__endswith=suffix).count(), 0
        )
        self.assertEqual(
            Organization.objects.filter(slug__startswith=flow.ORG_SLUG_PREFIX).count(), 0
        )
        self.assertEqual(Membership.all_objects.count(), 0)


# ---------------------------------------------------------------------------
# The happy path — the baseline every failure test is measured against
# ---------------------------------------------------------------------------


@override_settings(**SMTP_SETTINGS)
class HappyPathTests(HealthcheckTestCase):
    def test_a_healthy_deployment_exits_zero_with_every_step_ok(self):
        raised, summary, _, _ = self.run_check()
        failures = [entry for entry in summary["steps"] if not entry["ok"]]
        self.assertEqual(failures, [], f"unexpected failures: {failures}")
        self.assertIsNone(raised)
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["passed"], len(summary["steps"]))

    def test_it_leaves_the_database_exactly_as_it_found_it(self):
        self.run_check()
        self.assertSentinelDatabaseIsEmpty()

    def test_running_it_twice_accumulates_nothing(self):
        for _ in range(2):
            cache.clear()
            raised, summary, _, _ = self.run_check()
            self.assertIsNone(raised, [s for s in summary["steps"] if not s["ok"]])
            self.assertSentinelDatabaseIsEmpty()

    def test_the_login_really_happened(self):
        """Step 6 must have created a user, a workspace and an owner membership.

        Asserted through the *reported identity* rather than by reading the
        database afterwards, because by then cleanup has correctly removed it.
        """
        _, summary, _, _ = self.run_check()
        identity = self.step(summary, 6)["detail"]["identity"]
        self.assertEqual(identity["email"], flow.LOGIN_ADDRESS)
        self.assertEqual(identity["membership_role"], "owner")
        self.assertTrue(identity["org_id"])

    def test_step_4_walks_the_envelope_and_transmits_no_message(self):
        """AUTH, MAIL FROM, RCPT TO, RSET, QUIT — and never DATA.

        The envelope is the point: Brevo accepts AUTH for a credential that is
        not permitted to send as ``DEFAULT_FROM_EMAIL``, and only MAIL FROM or
        RCPT TO reveals it. RSET abandons the transaction, so nothing is queued
        and nobody is mailed — ``sendmail``/``send_message`` on the double raise
        if they are ever reached.
        """
        _, summary, _, _ = self.run_check()
        self.assertTrue(self.step(summary, 4)["ok"])
        self.assertEqual(len(FakeSMTP.instances), 1)
        conversation = FakeSMTP.instances[0]
        self.assertEqual(
            conversation.events,
            ["ehlo", "starttls", "ehlo", "login", "mail", "rcpt", "rset", "quit"],
        )
        # The envelope sender must be the bare addr-spec, never the formatted
        # header: a display name in MAIL FROM is a syntax error.
        self.assertEqual(conversation.envelope_sender, "codes@findajob.test")
        self.assertEqual(conversation.envelope_recipient, flow.LOGIN_ADDRESS)

    def test_a_sender_the_relay_refuses_fails_step_4(self):
        """The outage of 2026-08-02: AUTH fine, sender not allowed, 202 anyway."""
        try:
            FakeSMTP.mail_reply = (550, b"Sender not verified")
            raised, summary, _, _ = self.run_check()
        finally:
            FakeSMTP.mail_reply = (250, b"sender ok")
        entry = self.assertFailedWithStep(raised, summary, 4)
        self.assertIn("MAIL FROM", entry["evidence"])

    def test_the_smtp_password_is_never_printed(self):
        _, _, stdout, stderr = self.run_check()
        secret = SMTP_SETTINGS["EMAIL_HOST_PASSWORD"]
        self.assertNotIn(secret, stdout)
        self.assertNotIn(secret, stderr)

    def test_the_plaintext_otp_code_is_never_printed(self):
        """Step 5 mints a real code; the report must describe it, not reveal it.

        Spec §4: the plaintext code is never stored, logged or put in a payload.
        ``manage.py issue_login_code`` is the *one* command allowed to print it —
        this one is not.
        """
        minted: list[str] = []
        real_issue_code = flow.issue_code

        def capture(**kwargs):
            otp, code = real_issue_code(**kwargs)
            minted.append(code)
            return otp, code

        with mock.patch.object(flow, "issue_code", capture):
            _, summary, stdout, stderr = self.run_check()

        self.assertEqual(len(minted), 1)
        self.assertNotIn(minted[0], stdout)
        self.assertNotIn(minted[0], stderr)
        self.assertIn("code not printed", self.step(summary, 5)["evidence"])

    def test_the_human_report_goes_to_stderr_under_json(self):
        _, _, stdout, stderr = self.run_check()
        json.loads(stdout)  # stdout is pure JSON and nothing else
        self.assertIn("check_login_flow", stderr)
        self.assertIn("OK", stderr)

    def test_without_json_the_report_goes_to_stdout(self):
        site = FakeSite()
        out, err = StringIO(), StringIO()
        with mock.patch.object(flow.Command, "transport", staticmethod(site)), \
                mock.patch.object(flow.smtplib, "SMTP", FakeSMTP):
            call_command("check_login_flow", base=BASE, stdout=out, stderr=err)
        self.assertIn("cleanup", out.getvalue())
        self.assertIn("PASS", out.getvalue())


# ---------------------------------------------------------------------------
# It must never mail a human
# ---------------------------------------------------------------------------


@override_settings(**SMTP_SETTINGS)
class NoMailToHumansTests(HealthcheckTestCase):
    def test_the_mailer_is_never_asked_for_a_connection(self):
        """Both request-code addresses are suppressed, so no send path is entered.

        ``apps.accounts.mailer.get_connection`` is the last thing before the wire.
        If it is ever reached, this command has mailed a domain that can never
        resolve and Brevo records a hard bounce.
        """
        from apps.accounts import mailer

        with mock.patch.object(
            mailer, "get_connection",
            side_effect=AssertionError("the healthcheck opened a mail connection"),
        ):
            raised, summary, _, _ = self.run_check()
        self.assertIsNone(raised, [s for s in summary["steps"] if not s["ok"]])
        self.assertEqual(len(mail.outbox), 0)

    def test_the_module_contains_no_call_that_could_transmit_a_message(self):
        """Read the AST, not the prose: the docstring discusses mail at length.

        Anything that could put a message on the wire — ``send_mail``,
        ``send_login_code``, a backend's ``send_messages``, or smtplib's own
        ``sendmail`` / ``send_message`` — must not be *called* anywhere in this
        module. ``smtplib`` is imported on purpose (step 4 authenticates), so an
        import check would be useless; a call check is not.
        """
        import ast
        from pathlib import Path

        forbidden = {
            "send_mail", "send_login_code", "send_messages", "sendmail",
            "send_message", "EmailMessage", "EmailMultiAlternatives",
        }
        tree = ast.parse(Path(flow.__file__).read_text(encoding="utf-8"))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name in forbidden:
                    called.add(name)
        self.assertEqual(called, set())

    def test_it_suppresses_the_addresses_it_is_about_to_post(self):
        seen = {}

        def hook(site, method, path, headers, body):
            if path == flow.REQUEST_CODE_PATH:
                address = json.loads(body)["email"]
                seen[address] = SuppressedAddress.is_suppressed(address)
            return None

        self.run_check(FakeSite(hook=hook))
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(seen.values()), seen)


# ---------------------------------------------------------------------------
# Failure tests — one per property the command claims to check
# ---------------------------------------------------------------------------


@override_settings(**SMTP_SETTINGS)
class PublicSurfaceFailureTests(HealthcheckTestCase):
    def test_a_redirected_public_page_fails(self):
        site = FakeSite(
            static={"/app.html": (302, [("Location", "/auth.html")], b"")}
        )
        raised, summary, _, _ = self.run_check(site)
        entry = self.assertFailedWithStep(raised, summary, 1)
        self.assertIn("redirected", entry["evidence"])

    def test_a_public_page_behind_basic_auth_fails(self):
        site = FakeSite(
            static={
                "/auth.html": (
                    401, [("WWW-Authenticate", 'Basic realm="findajob.pro admin"')], b"",
                )
            }
        )
        raised, summary, _, _ = self.run_check(site)
        entry = self.assertFailedWithStep(raised, summary, 1)
        self.assertIn("WWW-Authenticate", entry["evidence"])

    def test_the_demo_variant_is_checked_separately(self):
        site = FakeSite(static={"/app.html?demo=1": (403, [], b"forbidden")})
        raised, summary, _, _ = self.run_check(site)
        entry = self.assertFailedWithStep(raised, summary, 1)
        self.assertIn("demo=1", entry["evidence"])


@override_settings(**SMTP_SETTINGS)
class CsrfFailureTests(HealthcheckTestCase):
    def test_a_csrf_endpoint_that_sets_no_cookie_fails(self):
        def hook(site, method, path, headers, body):
            if path == flow.CSRF_PATH:
                return 200, [("Content-Type", "application/json")], b'{"detail":"ok"}'
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 2)
        self.assertIn("csrftoken", entry["evidence"])

    def test_an_http_only_csrf_cookie_fails(self):
        """``web/api.js`` has to read this cookie from JavaScript (spec §8)."""

        def hook(site, method, path, headers, body):
            if path == flow.CSRF_PATH:
                return (
                    200,
                    [
                        ("Content-Type", "application/json"),
                        ("Set-Cookie", "csrftoken=abcdef123456; Path=/; HttpOnly"),
                    ],
                    b'{"detail":"ok"}',
                )
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 2)
        self.assertIn("HttpOnly", entry["evidence"])


@override_settings(**SMTP_SETTINGS)
class EnumerationParityFailureTests(HealthcheckTestCase):
    def test_a_different_status_for_a_known_address_fails(self):
        def hook(site, method, path, headers, body):
            if path == flow.REQUEST_CODE_PATH:
                address = json.loads(body)["email"]
                if address.startswith(flow.PARITY_KNOWN_PREFIX):
                    return (
                        200,
                        [("Content-Type", "application/json")],
                        b'{"masked_email":"f\\u2022\\u2022\\u2022@x","expires_in":600,'
                        b'"resend_in":60}',
                    )
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 3)
        self.assertIn("ENUMERATION LEAK", entry["evidence"])

    def test_a_different_body_shape_fails(self):
        def hook(site, method, path, headers, body):
            if path == flow.REQUEST_CODE_PATH:
                address = json.loads(body)["email"]
                if address.startswith(flow.PARITY_KNOWN_PREFIX):
                    # Same 202, one extra key. This is the realistic leak: an
                    # "account_exists" or "next" hint added for the frontend.
                    return (
                        202,
                        [("Content-Type", "application/json")],
                        b'{"masked_email":"a@b","expires_in":600,"resend_in":60,'
                        b'"account_exists":true}',
                    )
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 3)
        self.assertIn("shapes differ", entry["evidence"])

    def test_matching_but_non_202_statuses_fail(self):
        """Two identical 503s are parity, but they prove nothing."""

        def hook(site, method, path, headers, body):
            if path == flow.REQUEST_CODE_PATH:
                return (
                    503,
                    [("Content-Type", "application/json")],
                    b'{"error":{"code":"delivery_unavailable","detail":"nope"}}',
                )
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 3)
        self.assertIn("parity is untested", entry["evidence"])

    def test_a_429_is_diagnosed_as_a_budget_not_as_a_leak(self):
        """The signal that must never cry wolf.

        A 429 on one side only is a *shared* budget (per-IP, global) emptying
        between the two calls, not a difference drawn from the address. Calling
        that an enumeration leak would train everyone to ignore the message that
        matters most.
        """

        def hook(site, method, path, headers, body):
            if path == flow.REQUEST_CODE_PATH:
                address = json.loads(body)["email"]
                if address.startswith(flow.PROBE_PREFIX):
                    return (
                        429,
                        [("Content-Type", "application/json")],
                        b'{"error":{"code":"rate_limited","detail":"slow down",'
                        b'"retry_after":42}}',
                    )
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 3)
        self.assertIn("rate limited mid-comparison", entry["evidence"])
        self.assertNotIn("ENUMERATION LEAK", entry["evidence"])


class SmtpFailureTests(HealthcheckTestCase):
    @override_settings(**SMTP_SETTINGS)
    def test_a_rejected_credential_fails(self):
        class Rejecting(FakeSMTP):
            def login(self, user, password, **kwargs):
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")

        raised, summary, _, _ = self.run_check(smtp=Rejecting)
        entry = self.assertFailedWithStep(raised, summary, 4)
        self.assertIn("SMTPAuthenticationError", entry["evidence"])

    @override_settings(**SMTP_SETTINGS)
    def test_an_unreachable_relay_fails(self):
        class Unreachable(FakeSMTP):
            def __init__(self, *args, **kwargs):
                raise OSError("Name or service not known")

        raised, summary, _, _ = self.run_check(smtp=Unreachable)
        self.assertFailedWithStep(raised, summary, 4)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_a_console_email_backend_fails(self):
        """The exact regression spec §6's boot check exists to prevent."""
        raised, summary, _, _ = self.run_check()
        entry = self.assertFailedWithStep(raised, summary, 4)
        self.assertIn("EMAIL_BACKEND", entry["evidence"])

    @override_settings(**{**SMTP_SETTINGS, "EMAIL_HOST_PASSWORD": ""})
    def test_an_empty_credential_fails(self):
        raised, summary, _, _ = self.run_check()
        entry = self.assertFailedWithStep(raised, summary, 4)
        self.assertIn("EMAIL_HOST_USER", entry["evidence"])


@override_settings(**SMTP_SETTINGS)
class VerifyAndSessionFailureTests(HealthcheckTestCase):
    def test_unseeded_legal_documents_fail_the_verify_step(self):
        """No ``seed_legal`` on this database = no customer can ever register."""
        from apps.legal.models import LegalDocument

        LegalDocument.objects.all().delete()
        raised, summary, _, _ = self.run_check()
        entry = self.assertFailedWithStep(raised, summary, 6)
        self.assertIn("503", entry["evidence"])

    def test_a_me_payload_missing_a_contract_key_fails(self):
        def hook(site, method, path, headers, body):
            if path == flow.ME_PATH and method == "GET":
                status, headers_out, payload = FakeSite.django(method, path, headers, body)
                if status == 200:
                    data = json.loads(payload)
                    data.pop("membership_role", None)
                    return status, headers_out, json.dumps(data).encode()
                return status, headers_out, payload
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 7)
        self.assertIn("membership_role", entry["evidence"])

    def test_a_replayable_code_fails(self):
        """The most serious finding the command can make: single use is broken."""
        state = {"verifies": 0}

        def hook(site, method, path, headers, body):
            if path == flow.VERIFY_CODE_PATH and method == "POST":
                state["verifies"] += 1
                if state["verifies"] == 2:
                    return (
                        200,
                        [("Content-Type", "application/json")],
                        b'{"user":{},"org":{},"membership_role":"owner",'
                        b'"memberships":[],"lang":"en"}',
                    )
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 8)
        self.assertIn("SINGLE-USE BROKEN", entry["evidence"])

    def test_a_replay_that_never_reached_django_is_not_a_pass(self):
        """"Not 200" is not evidence. Measured against production: pointing the
        command at the www hostname made every API call a 308, and the earlier
        ``!= 200`` version of this step reported OK on it."""
        state = {"verifies": 0}

        for status, body in (
            (308, b""),                                   # wrong hostname
            (502, b"<html>bad gateway</html>"),           # api container down
            (404, b'{"error":{"code":"not_found","detail":"nope"}}'),   # route moved
            (429, b'{"error":{"code":"rate_limited","detail":"slow"}}'),  # not evaluated
        ):
            with self.subTest(status=status):
                state["verifies"] = 0

                def hook(site, method, path, headers, body_in, _s=status, _b=body):
                    if path == flow.VERIFY_CODE_PATH and method == "POST":
                        state["verifies"] += 1
                        if state["verifies"] == 2:
                            return _s, [("Content-Type", "application/json")], _b
                    return None

                raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
                entry = self.assertFailedWithStep(raised, summary, 8)
                self.assertIn("UNTESTED", entry["evidence"])

    def test_a_session_that_survives_logout_fails(self):
        def hook(site, method, path, headers, body):
            if path == flow.LOGOUT_PATH and method == "POST":
                # 204, but the session cookie is never retired.
                return 204, [], b""
            return None

        raised, summary, _, _ = self.run_check(FakeSite(hook=hook))
        entry = self.assertFailedWithStep(raised, summary, 9)
        self.assertIn("logout did not destroy the session", entry["evidence"])


@override_settings(**SMTP_SETTINGS)
class AdminGateFailureTests(HealthcheckTestCase):
    def test_an_open_admin_fails(self):
        site = FakeSite(static={"/admin/": (302, [("Location", "/admin/login/")], b"")})
        raised, summary, _, _ = self.run_check(site)
        entry = self.assertFailedWithStep(raised, summary, 10)
        self.assertIn("expected 401", entry["evidence"])

    def test_a_401_without_a_challenge_header_fails(self):
        site = FakeSite(static={"/admin/": (401, [], b"nope")})
        raised, summary, _, _ = self.run_check(site)
        entry = self.assertFailedWithStep(raised, summary, 10)
        self.assertIn("no WWW-Authenticate", entry["evidence"])

    def test_a_loose_traefik_prefix_fails(self):
        """``PathPrefix(`/admin`)`` instead of ``/admin/`` also catches /adminx."""
        site = FakeSite(
            static={"/adminx": (401, [("WWW-Authenticate", "Basic realm=x")], b"")}
        )
        raised, summary, _, _ = self.run_check(site)
        entry = self.assertFailedWithStep(raised, summary, 10)
        self.assertIn("loosened", entry["evidence"])

    def test_a_redirecting_decoy_is_inconclusive_not_a_pass(self):
        """A 3xx on the decoy means the prefix rule was never actually observed."""
        site = FakeSite(
            static={"/adminx": (301, [("Location", "https://findajob.pro/adminx")], b"")}
        )
        raised, summary, _, _ = self.run_check(site)
        entry = self.assertFailedWithStep(raised, summary, 10)
        self.assertIn("UNTESTED", entry["evidence"])


@override_settings(**SMTP_SETTINGS)
class UnreachableSiteTests(HealthcheckTestCase):
    def test_a_dead_network_is_a_failure_not_a_skip(self):
        import urllib.error

        site = FakeSite(explode=urllib.error.URLError("Name or service not known"))
        raised, summary, _, _ = self.run_check(site)
        self.assertIsNotNone(raised)
        self.assertEqual(raised.returncode, 1)
        for number in (1, 2, 3, 6, 7, 8, 9, 10):
            entry = self.step(summary, number)
            self.assertFalse(entry["ok"], f"step {number} claimed success offline")
            self.assertNotIn("skip", entry["evidence"].lower())

    def test_cleanup_still_runs_when_the_site_is_down(self):
        import urllib.error

        site = FakeSite(explode=urllib.error.URLError("down"))
        _, summary, _, _ = self.run_check(site)
        self.assertTrue(self.step(summary, 11)["ok"], self.step(summary, 11))
        self.assertSentinelDatabaseIsEmpty()


@override_settings(**SMTP_SETTINGS)
class CleanupFailureTests(HealthcheckTestCase):
    def test_a_cleanup_that_deletes_nothing_fails(self):
        """The post-cleanup assertion is real, not decoration."""
        empty = {
            "users": 0, "otps": 0, "suppressed": 0, "memberships": 0,
            "organizations": 0, "audit_logs": 0, "consents": 0, "sessions": 0,
        }
        original = flow.Command._purge
        calls = {"n": 0}

        def purge_once_then_stop(self):
            calls["n"] += 1
            if calls["n"] == 1:  # let the preflight purge work
                return original(self)
            return dict(empty)

        with mock.patch.object(flow.Command, "_purge", purge_once_then_stop):
            raised, summary, _, _ = self.run_check()
        entry = self.assertFailedWithStep(raised, summary, 11)
        self.assertIn("left rows behind", entry["evidence"])

    def test_it_removes_the_organization_which_nothing_cascades_to(self):
        """``Organization`` has no FK to ``User``, so only explicit deletion works."""
        self.run_check()
        self.assertEqual(
            Organization.objects.filter(slug__startswith=flow.ORG_SLUG_PREFIX).count(), 0
        )

    def test_an_orphaned_organization_from_a_crashed_run_is_swept(self):
        Organization.objects.create(
            name="findajob-healthcheck", slug=f"{flow.ORG_SLUG_PREFIX}-99"
        )
        raised, summary, _, _ = self.run_check()
        self.assertIsNone(raised, [s for s in summary["steps"] if not s["ok"]])
        self.assertSentinelDatabaseIsEmpty()


@override_settings(**SMTP_SETTINGS)
class PreflightRefusalTests(HealthcheckTestCase):
    def test_it_refuses_when_the_sentinel_user_carries_foreign_data(self):
        """The guard that keeps this command from ever deleting real data."""
        user = User.objects.create_user(email=flow.LOGIN_ADDRESS)
        LogEntry.objects.create(
            user=user, object_repr="something real", action_flag=1, change_message=""
        )

        raised, summary, _, _ = self.run_check()

        entry = self.assertFailedWithStep(raised, summary, 0)
        self.assertIn("REFUSING TO RUN", entry["evidence"])
        self.assertIn("admin.logentry", entry["evidence"].lower())
        # Nothing was deleted.
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertEqual(LogEntry.objects.filter(user=user).count(), 1)

    def test_every_later_step_is_reported_as_failed_not_skipped(self):
        user = User.objects.create_user(email=flow.LOGIN_ADDRESS)
        LogEntry.objects.create(
            user=user, object_repr="x", action_flag=1, change_message=""
        )
        _, summary, _, _ = self.run_check()
        for number in range(1, 12):
            entry = self.step(summary, number)
            self.assertFalse(entry["ok"])
            self.assertIn("did not run", entry["evidence"])
        self.assertEqual(summary["passed"], 0)

    def test_a_staff_flag_on_the_sentinel_address_refuses(self):
        User.objects.create_user(
            email=f"left-over@{flow.SENTINEL_DOMAIN}", is_staff=True
        )
        raised, summary, _, _ = self.run_check()
        entry = self.assertFailedWithStep(raised, summary, 0)
        self.assertIn("is_staff", entry["evidence"])

    def test_an_organization_with_an_outside_member_is_never_deleted(self):
        """A shared workspace must survive, and its presence must be a failure."""
        sentinel = User.objects.create_user(email=flow.LOGIN_ADDRESS)
        outsider = User.objects.create_user(email="real.customer@example.de")
        org = Organization.objects.create(name="Shared", slug="shared-workspace")
        Membership.objects.create(org=org, user=sentinel, role="owner")
        Membership.objects.create(org=org, user=outsider, role="member")

        raised, summary, _, _ = self.run_check()

        self.assertIsNotNone(raised)
        self.assertIn(
            "refusing to delete", self.step(summary, 0)["evidence"].lower()
        )
        self.assertTrue(Organization.objects.filter(pk=org.pk).exists())
        self.assertTrue(User.objects.filter(pk=outsider.pk).exists())


@override_settings(**SMTP_SETTINGS)
class ContractTests(HealthcheckTestCase):
    def test_the_sentinel_domain_can_never_be_real(self):
        """RFC 2606 §2 — ``.invalid`` is guaranteed never to be delegated."""
        self.assertTrue(flow.SENTINEL_DOMAIN.endswith(".invalid"))
        _, summary, _, _ = self.run_check()
        for address in summary["identities"].values():
            self.assertTrue(
                address.endswith(f"@{flow.SENTINEL_DOMAIN}"), address
            )

    def test_the_two_parity_addresses_are_fresh_on_every_run(self):
        """Regression: a fixed parity address collides with its own cooldown.

        ``request-code`` carries a 60 s per-address resend cooldown and a 5/hour
        per-address budget (spec §5). A reused "known" address means the second
        run inside a minute answers 429 for the registered side and 202 for the
        random one — which step 3 would have reported as an enumeration leak.
        """
        _, first, _, _ = self.run_check()
        _, second, _, _ = self.run_check()
        self.assertNotEqual(
            first["identities"]["parity_known"], second["identities"]["parity_known"]
        )
        self.assertNotEqual(
            first["identities"]["parity_probe"], second["identities"]["parity_probe"]
        )
        # …and the login identity is deliberately stable: it never touches
        # request-code, and a successful verify resets the budget step 8 spends.
        self.assertEqual(first["identities"]["login"], second["identities"]["login"])

    def test_two_runs_inside_the_resend_cooldown_both_pass(self):
        """The same regression, from the outside: no cache clearing between runs."""
        raised_first, first, _, _ = self.run_check()
        raised_second, second, _, _ = self.run_check()  # cache deliberately warm
        self.assertIsNone(raised_first, [s for s in first["steps"] if not s["ok"]])
        self.assertIsNone(raised_second, [s for s in second["steps"] if not s["ok"]])
        self.assertSentinelDatabaseIsEmpty()

    def test_break_glass_uses_the_service_primitive(self):
        from apps.accounts.services import issue_code

        self.assertIs(flow.issue_code, issue_code)

    def test_the_json_summary_documents_the_budget_it_consumed(self):
        _, summary, _, _ = self.run_check()
        budget = summary["budget_consumed"]
        self.assertEqual(budget["request_code_calls"], 2)
        self.assertEqual(budget["emails_sent_to_a_human"], 0)

    def test_only_two_request_code_calls_are_made(self):
        site = FakeSite()
        self.run_check(site)
        posts = [
            call for call in site.calls
            if call == ("POST", flow.REQUEST_CODE_PATH)
        ]
        self.assertEqual(len(posts), 2, site.calls)

    def test_json_output_is_parseable_even_when_the_run_fails(self):
        site = FakeSite(static={"/": (500, [], b"boom")})
        raised, summary, stdout, _ = self.run_check(site)
        self.assertIsNotNone(raised)
        json.loads(stdout)
        self.assertFalse(summary["ok"])
        self.assertEqual(summary["failed"], 1)


class JsonShapeTests(TestCase):
    """The primitive the enumeration-parity test is built on."""

    def test_identical_structures_with_different_values_match(self):
        left = {"masked_email": "a•••@x.de", "expires_in": 600, "resend_in": 60}
        right = {"masked_email": "z•••@y.de", "expires_in": 600, "resend_in": 60}
        self.assertEqual(flow.json_shape(left), flow.json_shape(right))

    def test_an_extra_key_is_a_different_shape(self):
        base = {"a": 1}
        self.assertNotEqual(flow.json_shape(base), flow.json_shape({"a": 1, "b": 2}))

    def test_a_type_change_is_a_different_shape(self):
        self.assertNotEqual(flow.json_shape({"org": None}), flow.json_shape({"org": {}}))

    def test_list_length_is_not_shape(self):
        self.assertEqual(
            flow.json_shape({"m": [{"id": 1}]}),
            flow.json_shape({"m": [{"id": 1}, {"id": 2}]}),
        )

    def test_an_empty_list_differs_from_a_populated_one(self):
        self.assertNotEqual(
            flow.json_shape({"m": []}), flow.json_shape({"m": [{"id": 1}]})
        )


class ScrubTests(TestCase):
    @override_settings(EMAIL_HOST_PASSWORD="super-secret-smtp-key")
    def test_configured_secrets_are_redacted(self):
        self.assertEqual(
            flow.scrub("AUTH failed for super-secret-smtp-key"),
            "AUTH failed for [redacted]",
        )

    @override_settings(EMAIL_HOST_PASSWORD="abc")
    def test_a_short_value_is_left_alone(self):
        """Redacting a 3-character value would mangle unrelated text."""
        self.assertEqual(flow.scrub("abcdef"), "abcdef")
