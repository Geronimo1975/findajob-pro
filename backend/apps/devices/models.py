"""``TrustedDevice`` — a machine the user has explicitly asked us to remember.

See ``docs/2026-08-22-trusted-device-spec.md``. The rule that outranks everything
else in this module is the same one ``apps.accounts.models`` states about OTP
codes: **the plaintext secret is never stored**. :func:`hash_secret` is the only
way a secret reaches the database, and it is a one-way HMAC keyed by
``SECRET_KEY``.

This is not a second factor. A cookie that mints a session is, in raw strength,
a long-lived session cookie — whoever holds it holds the account. What it buys
over simply raising ``SESSION_COOKIE_AGE`` is that it is enumerable and
revocable *per device*, which is what makes a self-service device list possible.

Django 6.0: ``CheckConstraint`` takes ``condition=`` (``check=`` was removed) and
``Model.save()`` no longer accepts positional arguments.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.translation import gettext_lazy as _

#: How long a trusted device survives before the user must do a full OTP login
#: again. Spec §2.
TRUSTED_DEVICE_TTL_DAYS = 30

#: Bytes of entropy in the cookie's secret half. 32 bytes via
#: ``secrets.token_urlsafe`` is ~43 characters and far past brute force — unlike
#: the 6-digit OTP, there is no keyspace argument for keeping this small.
DEVICE_SECRET_BYTES = 32

#: Key salt for :func:`django.utils.crypto.salted_hmac`. Changing this string
#: invalidates every trusted device, which is a legitimate emergency lever.
DEVICE_HMAC_KEY_SALT = "findajob.device"

#: sha256, not the ``salted_hmac`` default of sha1: 64 hex characters is exactly
#: what ``TrustedDevice.secret_hash`` is sized for.
DEVICE_HMAC_ALGORITHM = "sha256"

MAX_DEVICE_USER_AGENT_LENGTH = 400
MAX_DEVICE_LABEL_LENGTH = 120

#: Separates the two halves of the cookie value: ``{device_id}.{secret}``.
#: A dot cannot occur in a UUID and is not produced by ``token_urlsafe``, so the
#: split is unambiguous.
COOKIE_VALUE_SEPARATOR = "."


def hash_secret(secret: str, *, device_id) -> str:
    """One-way HMAC of a device secret. 64 lowercase hex characters.

    ``device_id`` is mixed into the key salt so a hash captured for one device
    cannot be replayed against another, mirroring how
    ``apps.accounts.models.hash_code`` mixes in the email address.
    """
    key_salt = f"{DEVICE_HMAC_KEY_SALT}:{device_id}"
    return salted_hmac(
        key_salt, str(secret), algorithm=DEVICE_HMAC_ALGORITHM
    ).hexdigest()


class TrustedDeviceQuerySet(models.QuerySet):
    """Queryset helpers for the one query that matters: *is this device live?*"""

    def live(self, *, user=None, now=None) -> "TrustedDeviceQuerySet":
        """Rows that could still mint a session right now.

        Live means all three of: never revoked, not past ``expires_at``, and —
        when ``user`` is given — belonging to that user.
        """
        now = now or timezone.now()
        queryset = self.filter(revoked_at__isnull=True, expires_at__gt=now)
        if user is not None:
            queryset = queryset.filter(user=user)
        return queryset


class TrustedDevice(models.Model):
    """One remembered machine. The cookie's secret is **not** in this table."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="trusted_devices",
        verbose_name=_("user"),
    )
    #: The public half of the cookie. Safe to log, safe to show in a UI.
    device_id = models.UUIDField(
        _("device id"), default=uuid.uuid4, unique=True, editable=False
    )
    secret_hash = models.CharField(_("secret hash"), max_length=64)

    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    expires_at = models.DateTimeField(_("expires at"))
    last_used_at = models.DateTimeField(_("last used at"), null=True, blank=True)
    revoked_at = models.DateTimeField(_("revoked at"), null=True, blank=True)

    #: Display only — what the device list shows so a user can recognise it.
    user_agent = models.CharField(
        _("user agent"), max_length=MAX_DEVICE_USER_AGENT_LENGTH, blank=True
    )
    ip = models.GenericIPAddressField(_("ip address"), null=True, blank=True)
    label = models.CharField(
        _("label"), max_length=MAX_DEVICE_LABEL_LENGTH, blank=True
    )

    objects = TrustedDeviceQuerySet.as_manager()

    class Meta:
        verbose_name = _("trusted device")
        verbose_name_plural = _("trusted devices")
        ordering = ("-last_used_at", "-created_at", "-id")
        indexes = [
            models.Index(fields=["user", "-created_at"], name="device_user_created_idx"),
        ]

    def __str__(self) -> str:
        # Never render the hash: it is treated as a secret.
        return f"{self.device_id} ({self.user_id})"

    # -- state ----------------------------------------------------------

    def is_expired(self, now=None) -> bool:
        return (now or timezone.now()) >= self.expires_at

    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    def is_live(self, now=None) -> bool:
        return not (self.is_revoked() or self.is_expired(now))

    def matches(self, secret: str) -> bool:
        """Constant-time comparison against the stored hash."""
        return constant_time_compare(
            self.secret_hash, hash_secret(secret, device_id=self.device_id)
        )
