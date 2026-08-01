"""Accounts models — ``User``, ``EmailOTP``, ``SuppressedAddress``.

Implementation spec §2 pins the boundary: this app owns **exactly two** models,
``User`` and ``EmailOTP``, plus ``SuppressedAddress``. It must not define,
import or instantiate ``Organization``, ``Membership`` or ``AuditLog`` (those
are ``apps.tenancy``, reachable only through
``apps.tenancy.services.bootstrap_workspace()``), nor ``Invitation``,
``Subscription`` or ``UsageQuota`` (build-order positions 5–7, not built).

Django 6.0 notes that are load-bearing here:

* ``CheckConstraint`` takes ``condition=`` — ``check=`` was removed.
* ``DEFAULT_AUTO_FIELD`` already defaults to ``BigAutoField``; the setting is
  deliberately not emitted anywhere, so every pk below is a ``BigAutoField``.
* ``Model.save()`` no longer accepts positional arguments.

The one rule that outranks everything else in this module: **the plaintext OTP
code is never stored**. :func:`hash_code` is the only way a code reaches the
database, and it is a one-way HMAC keyed by ``SECRET_KEY``.
"""

from __future__ import annotations

import unicodedata

from django.conf import settings
from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.validators import MaxValueValidator
from django.db import models
from django.utils import timezone
from django.utils.crypto import salted_hmac
from django.utils.translation import gettext_lazy as _

# ---------------------------------------------------------------------------
# Constants shared by services, views, tests and the management command
# ---------------------------------------------------------------------------

#: Length of the numeric code the user types. Six digits = 10^6 possibilities,
#: which is only safe because of the per-row attempt cap below.
OTP_CODE_LENGTH = 6

#: Wrong guesses a single row survives. On the sixth the row is dead — spec §4:
#: *"max 5, then the ROW dies (not the account)"*.
MAX_OTP_ATTEMPTS = 5

#: How many un-consumed, un-expired codes may exist for one address at once.
#: Spec §4: German providers greylist and then deliver two codes seconds apart,
#: and the user types the older one — so a resend must NOT supersede.
MAX_LIVE_OTPS = 3

#: Lifetime of a code, in seconds (spec §4: ``created_at + 10 min``).
OTP_TTL_SECONDS = 10 * 60

#: Seconds before the same address may request another code (spec §5).
OTP_RESEND_COOLDOWN_SECONDS = 60

PURPOSE_LOGIN = "login"

#: Key salt for :func:`django.utils.crypto.salted_hmac`. Changing this string
#: invalidates every live code, which is a legitimate emergency lever.
OTP_HMAC_KEY_SALT = "findajob.otp"

#: sha256, not the ``salted_hmac`` default of sha1: 64 hex characters is exactly
#: what ``EmailOTP.code_hash`` is sized for (spec §4, ``CharField(64)``).
OTP_HMAC_ALGORITHM = "sha256"

MAX_EMAIL_LENGTH = 254  # RFC 5321 practical maximum
MAX_SUPPRESSION_REASON_LENGTH = 64


def normalize_email(raw: object) -> str:
    """Return the canonical form of an email address.

    Spec §4 requires stored addresses to be *"lowercased, NFKC-normalised"*.
    This is the single definition of that, used by the user manager, the OTP
    rows, the suppression list and every rate-limit key — if two call sites
    disagreed about canonical form, a rate-limit budget could be bypassed by
    changing the case of one letter.

    Non-printable characters (NUL in particular — Django's
    ``ProhibitNullCharactersValidator`` is form-only and never runs at the model
    layer) are dropped rather than rejected, because this runs before
    validation.
    """
    text = "" if raw is None else str(raw)
    text = unicodedata.normalize("NFKC", text).strip()
    text = "".join(char for char in text if char.isprintable())
    return text.lower()


def hash_code(code: str, *, email: str | None = None) -> str:
    """One-way HMAC of a plaintext OTP code. 64 lowercase hex characters.

    Keyed by ``SECRET_KEY`` through :func:`~django.utils.crypto.salted_hmac`.
    Deliberately **not** PBKDF2 (spec §4): a 6-digit keyspace makes a slow hash
    pointless — an attacker with the database and the secret key can enumerate
    all 10^6 codes either way — while costing ~300 ms on every verify.

    ``email`` is mixed into the key salt when given, so a hash captured for one
    address cannot be replayed against another even if both were issued the
    same code.
    """
    key_salt = OTP_HMAC_KEY_SALT
    if email:
        key_salt = f"{OTP_HMAC_KEY_SALT}:{normalize_email(email)}"
    return salted_hmac(
        key_salt, str(code), algorithm=OTP_HMAC_ALGORITHM
    ).hexdigest()


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class UserManager(BaseUserManager):
    """Email-as-username manager.

    ``use_in_migrations`` is on so a data migration can create users, which is
    the documented Django behaviour for a swapped user model.
    """

    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra):
        email = normalize_email(email)
        if not email:
            raise ValueError("Users must have an email address.")
        user = self.model(email=email, **extra)
        if password:
            user.set_password(password)
        else:
            # OTP accounts have no password at all. An unusable password can
            # never match any input, including the empty string.
            user.set_unusable_password()
        user.full_clean(exclude=["password", "last_login"])
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        # A superuser logs into Django admin, which is password-based.
        extra.setdefault("is_verified", True)
        if extra["is_staff"] is not True:
            raise ValueError("Superuser must have is_staff=True.")
        if extra["is_superuser"] is not True:
            raise ValueError("Superuser must have is_superuser=True.")
        if not password:
            raise ValueError(
                "Superuser must have a password — Django admin has no OTP flow."
            )
        return self._create_user(email, password, **extra)

    def get_by_natural_key(self, username: str | None):
        """Look up by canonicalised email.

        Without the normalisation here, ``authenticate(username="A@B.DE")``
        would miss a row stored as ``a@b.de``.
        """
        return self.get(**{self.model.USERNAME_FIELD: normalize_email(username)})


class User(AbstractBaseUser, PermissionsMixin):
    """A person. Identified by email; there is no ``username`` field.

    Login is a 6-digit email OTP (spec §1). ``password`` exists only because
    Django admin needs one — every account created by the OTP flow gets an
    unusable password.
    """

    email = models.EmailField(
        _("email address"), max_length=MAX_EMAIL_LENGTH, unique=True
    )

    #: True once the address has been proven by a successful OTP verify.
    #: Surfaced by ``GET /api/v1/me/`` as ``user.is_verified`` (spec §3).
    is_verified = models.BooleanField(_("email verified"), default=False)

    #: Preferred UI language. Kept on the user, not only in a cookie, so the
    #: OTP email goes out in the right language on a device that never carried
    #: the cookie.
    lang = models.CharField(
        _("language"),
        max_length=8,
        choices=settings.LANGUAGES,
        default=settings.LANGUAGE_CODE,
    )

    is_active = models.BooleanField(_("active"), default=True)
    is_staff = models.BooleanField(_("staff status"), default=False)

    date_joined = models.DateTimeField(_("date joined"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: list[str] = []

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")
        ordering = ("email",)
        constraints = [
            # Django 6.0: `condition=`, not `check=` (removed).
            models.CheckConstraint(
                condition=~models.Q(email=""), name="user_email_not_blank"
            ),
        ]

    def __str__(self) -> str:
        return self.email

    def clean(self) -> None:
        super().clean()
        self.email = normalize_email(self.email)

    def save(self, **kwargs) -> None:
        # Django 6.0 removed positional arguments to Model.save().
        self.email = normalize_email(self.email)
        super().save(**kwargs)

    # Django admin templates call these on request.user.
    def get_full_name(self) -> str:
        return self.email

    def get_short_name(self) -> str:
        return self.email.split("@", 1)[0]


# ---------------------------------------------------------------------------
# EmailOTP
# ---------------------------------------------------------------------------


class EmailOTPQuerySet(models.QuerySet):
    """Queryset helpers for the one query that matters: *which codes are live?*"""

    def live(self, *, email: str | None = None, purpose: str | None = PURPOSE_LOGIN,
             now=None) -> "EmailOTPQuerySet":
        """Rows that could still be verified right now.

        Live means all three of: never consumed, not past ``expires_at``, and
        still under the attempt cap. A row failing any one of them is dead and
        is never compared against again.
        """
        now = now or timezone.now()
        queryset = self.filter(
            consumed_at__isnull=True,
            expires_at__gt=now,
            attempts__lt=MAX_OTP_ATTEMPTS,
        )
        if email is not None:
            queryset = queryset.filter(email=normalize_email(email))
        if purpose is not None:
            queryset = queryset.filter(purpose=purpose)
        return queryset

    def newest_first(self) -> "EmailOTPQuerySet":
        """Explicit ordering.

        Never rely on ``Meta.ordering`` for this: Postgres sorts ``DESC NULLS
        FIRST`` while SQLite sorts NULLs last, so an implicit ordering can pick
        a different row on each backend. ``created_at`` is non-nullable here, but
        the ``-id`` tie-break also makes two rows written in the same tick
        deterministic.
        """
        return self.order_by("-created_at", "-id")


class EmailOTP(models.Model):
    """One issued login code. The code itself is **not** in this table.

    Spec §4 verbatim::

        email            EmailField(db_index=True)      # lowercased, NFKC-normalised
        code_hash        CharField(64)                  # salted_hmac("findajob.otp", code).hexdigest()
        purpose          CharField  = "login"
        created_at       DateTimeField(auto_now_add)
        expires_at       DateTimeField                  # created_at + 10 min
        attempts         PositiveSmallIntegerField = 0  # max 5, then the ROW dies (not the account)
        consumed_at      DateTimeField(null=True)
        request_ip       GenericIPAddressField(null=True)

    Note there is no FK to :class:`User`. That is deliberate and load-bearing:
    ``request-code`` is branch-free (spec §6) and must behave identically for an
    address that has an account and one that does not, so it cannot look a user
    up — and a nullable FK that is usually null would tempt someone to.
    """

    email = models.EmailField(_("email address"), max_length=MAX_EMAIL_LENGTH, db_index=True)
    code_hash = models.CharField(_("code hash"), max_length=64)
    purpose = models.CharField(_("purpose"), max_length=32, default=PURPOSE_LOGIN)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    expires_at = models.DateTimeField(_("expires at"))
    attempts = models.PositiveSmallIntegerField(
        _("failed attempts"),
        default=0,
        validators=[MaxValueValidator(MAX_OTP_ATTEMPTS)],
    )
    consumed_at = models.DateTimeField(_("consumed at"), null=True, blank=True)
    request_ip = models.GenericIPAddressField(_("request ip"), null=True, blank=True)

    objects = EmailOTPQuerySet.as_manager()

    class Meta:
        verbose_name = _("email login code")
        verbose_name_plural = _("email login codes")
        ordering = ("-created_at", "-id")
        constraints = [
            # Spec §4, verbatim — note `condition=`, not `check=`.
            models.CheckConstraint(
                condition=models.Q(attempts__lte=MAX_OTP_ATTEMPTS),
                name="otp_attempts_bounded",
            ),
        ]
        indexes = [
            # Spec §4. Covers the only hot query: "live codes for this address,
            # newest first".
            models.Index(fields=["email", "-created_at"], name="otp_email_created_idx"),
        ]

    def __str__(self) -> str:
        # Never render the hash: a 6-digit preimage is trivially brute-forced by
        # anyone who also holds SECRET_KEY, so the hash is treated as a secret.
        return f"{self.email} ({self.purpose}, {self.created_at:%Y-%m-%d %H:%M})"

    # -- state ----------------------------------------------------------

    def is_expired(self, now=None) -> bool:
        return (now or timezone.now()) >= self.expires_at

    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    def is_exhausted(self) -> bool:
        return self.attempts >= MAX_OTP_ATTEMPTS

    def is_live(self, now=None) -> bool:
        return not (self.is_consumed() or self.is_expired(now) or self.is_exhausted())

    def matches(self, code: str) -> bool:
        """Constant-time comparison against the stored hash.

        Uses :func:`django.utils.crypto.constant_time_compare` (spec §4) so the
        time taken carries no information about how many leading digits were
        right.
        """
        from django.utils.crypto import constant_time_compare

        return constant_time_compare(
            self.code_hash, hash_code(code, email=self.email)
        )


# ---------------------------------------------------------------------------
# SuppressedAddress
# ---------------------------------------------------------------------------


class SuppressionReason(models.TextChoices):
    HARD_BOUNCE = "hard_bounce", _("Hard bounce")
    COMPLAINT = "complaint", _("Spam complaint")
    UNSUBSCRIBE = "unsubscribe", _("Unsubscribed")
    BLOCKED = "blocked", _("Blocked by the provider")
    MANUAL = "manual", _("Suppressed by hand")


class SuppressedAddress(models.Model):
    """An address we must stop mailing.

    Spec §6: ``request-code`` is branch-free, *"therefore it will mail
    non-existent addresses"* — so hard bounces and complaints have to be
    absorbed or the sending domain's reputation dies. Brevo posts them to
    ``POST /webhooks/email/brevo/`` (signature-verified, see
    ``apps.accounts.webhooks``) and ``issue_login_code`` skips suppressed
    addresses *while still returning 202*.
    """

    email = models.EmailField(
        _("email address"), max_length=MAX_EMAIL_LENGTH, unique=True
    )
    reason = models.CharField(
        _("reason"),
        max_length=MAX_SUPPRESSION_REASON_LENGTH,
        choices=SuppressionReason.choices,
        default=SuppressionReason.HARD_BOUNCE,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    class Meta:
        verbose_name = _("suppressed address")
        verbose_name_plural = _("suppressed addresses")
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:
        return f"{self.email} ({self.reason})"

    def save(self, **kwargs) -> None:
        self.email = normalize_email(self.email)
        super().save(**kwargs)

    @classmethod
    def is_suppressed(cls, email: str) -> bool:
        return cls.objects.filter(email=normalize_email(email)).exists()

    @classmethod
    def suppress(cls, email: str, reason: str = SuppressionReason.HARD_BOUNCE):
        """Idempotently record a suppression."""
        address = normalize_email(email)
        if not address:
            return None
        obj, created = cls.objects.get_or_create(
            email=address, defaults={"reason": reason}
        )
        if not created and obj.reason != reason:
            obj.reason = reason
            obj.save(update_fields=["reason", "updated_at"])
        return obj
