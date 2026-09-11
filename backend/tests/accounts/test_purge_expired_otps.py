"""``manage.py purge_expired_otps`` — GDPR retention purge for dead OTP rows.

The OTP TTL is 10 minutes (``OTP_TTL_SECONDS``), so any row older than the
retention window is guaranteed dead regardless of how it died — these tests
exercise the age boundary, not liveness, because age alone is the command's
only signal.
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from apps.accounts.models import EmailOTP, hash_code
from tests.accounts.support import AuthTestCase


def run(**kwargs) -> tuple[str, str]:
    out, err = StringIO(), StringIO()
    call_command("purge_expired_otps", stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


def make_otp(email: str, *, age_days: float, consumed: bool = False) -> EmailOTP:
    """An ``EmailOTP`` row backdated past ``auto_now_add`` via a queryset update."""
    now = timezone.now()
    otp = EmailOTP.objects.create(
        email=email,
        code_hash=hash_code("123456", email=email),
        expires_at=now + timedelta(minutes=10),
        consumed_at=now if consumed else None,
    )
    created_at = now - timedelta(days=age_days)
    EmailOTP.objects.filter(pk=otp.pk).update(
        created_at=created_at, expires_at=created_at + timedelta(minutes=10)
    )
    otp.refresh_from_db()
    return otp


class PurgeExpiredOtpsTests(AuthTestCase):
    def test_deletes_rows_older_than_the_default_seven_days(self):
        old = make_otp("george@example.de", age_days=8)
        make_otp("george@example.de", age_days=6)
        run()
        self.assertFalse(EmailOTP.objects.filter(pk=old.pk).exists())
        self.assertEqual(EmailOTP.objects.count(), 1)

    def test_leaves_recent_rows_alone_even_if_dead(self):
        # A consumed code from moments ago is dead but well within the window.
        make_otp("george@example.de", age_days=0, consumed=True)
        run()
        self.assertEqual(EmailOTP.objects.count(), 1)

    def test_respects_a_custom_days_argument(self):
        make_otp("george@example.de", age_days=10)
        run(days=14)
        self.assertEqual(EmailOTP.objects.count(), 1)
        run(days=7)
        self.assertEqual(EmailOTP.objects.count(), 0)

    def test_dry_run_deletes_nothing_but_reports_the_count(self):
        make_otp("george@example.de", age_days=8)
        out, _ = run(dry_run=True)
        self.assertIn("Would delete 1", out)
        self.assertEqual(EmailOTP.objects.count(), 1)

    def test_rejects_a_non_positive_days_value(self):
        with self.assertRaises(CommandError):
            run(days=0)
        with self.assertRaises(CommandError):
            run(days=-3)

    def test_reports_zero_when_nothing_is_old_enough(self):
        make_otp("george@example.de", age_days=1)
        out, _ = run()
        self.assertIn("Deleted 0", out)
