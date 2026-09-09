"""Purge dead ``EmailOTP`` rows past their retention window.

Not part of the original implementation spec — a GDPR data-minimisation
follow-up. The OTP TTL is 10 minutes (``OTP_TTL_SECONDS``), so any row older
than the retention window is guaranteed dead — expired, consumed or
exhausted, it does not matter which — and holds an email address with no
remaining login purpose::

    python manage.py purge_expired_otps                # deletes rows older than 7 days
    python manage.py purge_expired_otps --days 14
    python manage.py purge_expired_otps --dry-run       # report only, delete nothing

Deliberately leaves :class:`~apps.accounts.models.SuppressedAddress` alone:
that table exists to protect the sending domain's reputation indefinitely,
and purging it would let a bounced or complained-about address receive mail
again.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import EmailOTP

#: Days a dead row survives before deletion. The command still requires
#: `--days` to be at least 1 regardless of this default, so a mistyped 0
#: cannot turn into "delete every row created this instant".
DEFAULT_RETENTION_DAYS = 7


class Command(BaseCommand):
    help = (
        "Delete EmailOTP rows older than the retention window (default "
        f"{DEFAULT_RETENTION_DAYS} days). Every such row is already dead: the "
        "OTP TTL is 10 minutes, so age alone proves it can no longer be used."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--days",
            type=int,
            default=DEFAULT_RETENTION_DAYS,
            help=f"Retention window in days (default: {DEFAULT_RETENTION_DAYS}).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report how many rows would be deleted, without deleting them.",
        )

    def handle(self, *args, **options) -> None:
        days = options["days"]
        if days < 1:
            raise CommandError("--days must be at least 1.")

        cutoff = timezone.now() - timedelta(days=days)
        queryset = EmailOTP.objects.filter(created_at__lt=cutoff)

        if options["dry_run"]:
            count = queryset.count()
            self.stdout.write(
                f"Would delete {count} EmailOTP row(s) older than {days} "
                f"day(s) (dry run, nothing deleted)."
            )
            return

        count, _ = queryset.delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Deleted {count} EmailOTP row(s) older than {days} day(s)."
            )
        )
