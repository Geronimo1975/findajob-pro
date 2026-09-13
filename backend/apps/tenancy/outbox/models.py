"""Outbox models — the user's own mailbox, for send-as-user application mail.

See ``docs/2026-08-02-outbox-spec.md``. Application emails are sent from the
user's mailbox, never from findajob infrastructure; ``EmailSendLog`` (phase 3)
arrives together with the jobs app.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.tenancy.managers import OrgScopedManager


class Provider(models.TextChoices):
    SMTP = "smtp", _("SMTP")
    GMAIL = "gmail", _("Gmail")
    OUTLOOK = "outlook", _("Outlook")


class MailboxStatus(models.TextChoices):
    CONNECTED = "connected", _("connected")
    ERROR = "error", _("error")
    REVOKED = "revoked", _("revoked")


class MailboxConnection(models.Model):
    """One member's connected mailbox in one organization.

    Secrets live only in ``credentials_encrypted`` (Fernet, see
    :mod:`apps.tenancy.outbox.crypto`) and are never serialized back out.
    """

    org = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="mailboxes",
        verbose_name=_("organization"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="mailboxes",
        verbose_name=_("user"),
    )
    provider = models.CharField(
        _("provider"), max_length=16, choices=Provider.choices
    )
    email_address = models.EmailField(_("email address"))
    display_name = models.CharField(_("display name"), max_length=200, blank=True)
    credentials_encrypted = models.BinaryField(_("credentials (encrypted)"))
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=MailboxStatus.choices,
        default=MailboxStatus.CONNECTED,
    )
    last_verified_at = models.DateTimeField(_("last verified at"), null=True, blank=True)
    last_error_code = models.CharField(_("last error code"), max_length=64, blank=True)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    #: Guarded: reads must go through ``.for_org()`` / ``.for_user()``.
    objects = OrgScopedManager(user_field="user")
    #: Unguarded, for Django admin and management commands only.
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("mailbox connection")
        verbose_name_plural = _("mailbox connections")
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=["org", "user"], name="one_mailbox_per_member"
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - admin nicety
        return f"{self.email_address} ({self.provider})"
