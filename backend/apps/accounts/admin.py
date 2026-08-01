"""Django admin for accounts.

Three ModelAdmins, all of them defensive about one thing: **nothing here may
make a login code readable, guessable or forgeable.**

* ``code_hash`` is never displayed. It is an HMAC over a six-digit preimage, so
  anyone holding it *and* ``SECRET_KEY`` can enumerate the code in under a
  second. Treat it as the secret it is and keep it off the screen.
* ``EmailOTP`` rows cannot be added or edited from admin — a staff user who
  could write ``code_hash`` could mint themselves a working code for any
  address. Deleting is allowed (that is just an early expiry).
* ``User`` uses plain ``ModelAdmin``, not ``django.contrib.auth.admin.UserAdmin``:
  that class is built around a ``username`` field this model does not have.
  Password handling is delegated to Django's own change-password view via
  ``ReadOnlyPasswordHashField``-equivalent behaviour — the raw hash is simply
  not editable here.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.accounts.models import EmailOTP, SuppressedAddress, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "is_verified", "is_active", "is_staff", "lang", "date_joined")
    list_filter = ("is_verified", "is_active", "is_staff", "is_superuser", "lang")
    # Required by any other app that wants `autocomplete_fields = ("user",)`.
    search_fields = ("email",)
    ordering = ("email",)
    readonly_fields = ("date_joined", "updated_at", "last_login", "password")
    filter_horizontal = ("groups", "user_permissions")
    fieldsets = (
        (None, {"fields": ("email", "lang", "is_verified")}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Timestamps"), {"fields": ("last_login", "date_joined", "updated_at")}),
        # Shown read-only so staff can see *that* a password exists (admin
        # accounts) versus not (OTP-only accounts), without being able to set
        # one from here.
        (_("Credentials"), {"fields": ("password",), "classes": ("collapse",)}),
    )


@admin.register(EmailOTP)
class EmailOTPAdmin(admin.ModelAdmin):
    """Read-only. Useful for answering "did the code go out?" — nothing else."""

    list_display = (
        "email",
        "purpose",
        "created_at",
        "expires_at",
        "attempts",
        "consumed_at",
        "request_ip",
        "state",
    )
    list_filter = ("purpose", "created_at")
    search_fields = ("email",)
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    # `code_hash` is deliberately absent from every list, form and filter.
    fields = (
        "email",
        "purpose",
        "created_at",
        "expires_at",
        "attempts",
        "consumed_at",
        "request_ip",
    )
    readonly_fields = fields

    @admin.display(description=_("state"))
    def state(self, obj: EmailOTP) -> str:
        if obj.is_consumed():
            return _("used")
        if obj.is_exhausted():
            return _("burned")
        if obj.is_expired():
            return _("expired")
        return _("live")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(SuppressedAddress)
class SuppressedAddressAdmin(admin.ModelAdmin):
    """The one place a wrongly-suppressed address can be released.

    Deleting a row here is the supported un-suppress: the next
    ``request-code`` for that address will mail again. That is why
    :mod:`apps.accounts.webhooks` errs toward *not* suppressing — a false
    positive is a login lockout only a human can clear.
    """

    list_display = ("email", "reason", "created_at", "updated_at")
    list_filter = ("reason",)
    search_fields = ("email",)
    ordering = ("-created_at",)
    readonly_fields = ("created_at", "updated_at")
