"""``manage.py issue_login_code`` — the break-glass path (spec §6)."""

from __future__ import annotations

import re
from io import StringIO

from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.accounts.models import EmailOTP, SuppressedAddress, User
from tests.accounts.support import AuthTestCase, seed_legal_documents

CODE_LINE = re.compile(r"^\s*(\d{6})\s*$", re.MULTILINE)


def run(**kwargs) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    call_command("issue_login_code", stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


class IssueLoginCodeTests(AuthTestCase):
    def test_writes_a_row_and_prints_the_code(self):
        out, _ = run(email="george@example.de")
        match = CODE_LINE.search(out)
        self.assertIsNotNone(match, out)
        otp = EmailOTP.objects.get()
        self.assertTrue(otp.matches(match.group(1)))

    def test_sends_no_email(self):
        run(email="george@example.de")
        self.assertEqual(len(mail.outbox), 0)

    def test_the_printed_code_actually_logs_in(self):
        seed_legal_documents()
        out, _ = run(email="george@example.de")
        code = CODE_LINE.search(out).group(1)
        response = self.verify("george@example.de", code, accepts_terms=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(User.objects.filter(email="george@example.de").exists())

    def test_the_address_is_normalised(self):
        run(email="  George@Example.DE ")
        self.assertEqual(EmailOTP.objects.get().email, "george@example.de")

    def test_it_ignores_the_rate_limits(self):
        # The point of break-glass: it has to work when the cache is saturated
        # or unreachable and every budget says no.
        for _ in range(10):
            run(email="george@example.de")
        self.assertEqual(EmailOTP.objects.count(), 10)

    def test_it_still_honours_the_three_live_code_cap(self):
        for _ in range(5):
            run(email="george@example.de")
        self.assertEqual(EmailOTP.objects.live(email="george@example.de").count(), 3)

    def test_it_works_for_a_suppressed_address(self):
        SuppressedAddress.suppress("george@example.de")
        out, _ = run(email="george@example.de")
        self.assertIn("suppression list", out)
        self.assertEqual(EmailOTP.objects.count(), 1)

    def test_it_warns_that_verification_will_create_the_account(self):
        out, _ = run(email="new@example.de")
        self.assertIn("CREATES the account", out)

    def test_it_reports_an_existing_account(self):
        User.objects.create_user(email="george@example.de")
        out, _ = run(email="george@example.de")
        self.assertIn("existing", out)

    def test_a_malformed_address_is_a_command_error(self):
        with self.assertRaises(CommandError):
            run(email="not-an-email")
        self.assertEqual(EmailOTP.objects.count(), 0)

    def test_an_empty_address_is_a_command_error(self):
        with self.assertRaises(CommandError):
            run(email="   ")
