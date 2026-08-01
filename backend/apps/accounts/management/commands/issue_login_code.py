"""Break-glass login (implementation spec §6).

    python manage.py issue_login_code --email george@example.de

Writes a real :class:`~apps.accounts.models.EmailOTP` row and prints the code to
**stdout**. It mails nothing, consults no rate limit and ignores the suppression
list.

Spec §6 requires it to ship *"in the same commit as ``0001_initial``"*, and the
reason is not convenience. The login path has exactly one external dependency —
an email service provider — and every failure mode of that dependency (a
suspended sending domain, an expired SMTP key, a DNS record someone edited, an
IP allowlist that silently started rejecting us, spec §6.1) locks out **every
user including the founder**, with no way back in. This command is the way back
in. It runs on the box, so possessing it already implies full control of the
application and the database; it therefore adds no attack surface that an
attacker who can run it does not already have.

The printed code is the only place a plaintext code is ever emitted anywhere in
this codebase, and that is the entire point of the command.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.utils import timezone

from apps.accounts.models import (
    OTP_TTL_SECONDS,
    PURPOSE_LOGIN,
    SuppressedAddress,
    User,
    normalize_email,
)
from apps.accounts.services import issue_code


class Command(BaseCommand):
    help = (
        "Issue a login code for an address and print it to stdout. "
        "Sends no email; bypasses every rate limit. Break-glass only."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--email",
            required=True,
            help="The address to issue a code for (case-insensitive).",
        )
        parser.add_argument(
            "--purpose",
            default=PURPOSE_LOGIN,
            help=f"EmailOTP.purpose value (default: {PURPOSE_LOGIN}).",
        )

    def handle(self, *args, **options) -> None:
        address = normalize_email(options["email"])
        if not address:
            raise CommandError("--email is required.")
        try:
            validate_email(address)
        except ValidationError as exc:
            raise CommandError(f"{address!r} is not a valid email address.") from exc

        purpose = str(options["purpose"]).strip() or PURPOSE_LOGIN

        otp, code = issue_code(email=address, ip=None, purpose=purpose)

        # Context, so the operator knows what will happen when they use it.
        exists = User.objects.filter(email=address).exists()
        suppressed = SuppressedAddress.is_suppressed(address)
        minutes = max(1, OTP_TTL_SECONDS // 60)
        remaining = int((otp.expires_at - timezone.now()).total_seconds())

        self.stdout.write(self.style.SUCCESS(code))
        self.stdout.write("")
        self.stdout.write(f"  address    : {address}")
        self.stdout.write(f"  account    : {'existing' if exists else 'will be created on verify'}")
        self.stdout.write(f"  purpose    : {purpose}")
        self.stdout.write(f"  otp id     : {otp.pk}")
        self.stdout.write(
            f"  expires    : {otp.expires_at:%Y-%m-%d %H:%M:%S %Z} "
            f"(in ~{max(0, remaining // 60)} of {minutes} min)"
        )
        if suppressed:
            self.stdout.write(
                self.style.WARNING(
                    "  note       : this address is on the suppression list, so the "
                    "normal flow would not have mailed it."
                )
            )
        if not exists:
            self.stdout.write(
                self.style.WARNING(
                    "  note       : verifying this code CREATES the account, and the "
                    "verify request must carry accepts_terms=true."
                )
            )
        self.stdout.write("")
        self.stdout.write(
            "  No email was sent. The code above is not stored anywhere and "
            "cannot be recovered once this output is lost."
        )
