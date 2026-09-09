"""Read-mostly admin for trusted devices.

The one write affordance is revocation, which is the operator's half of the
self-service device list. ``secret_hash`` is never exposed: a stolen hash plus
``SECRET_KEY`` is a working credential, so it is treated as a secret.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.devices.models import TrustedDevice


@admin.register(TrustedDevice)
class TrustedDeviceAdmin(admin.ModelAdmin):
    list_display = ("device_id", "user", "created_at", "last_used_at", "expires_at", "revoked_at")
    list_filter = ("created_at", "expires_at", "revoked_at")
    search_fields = ("device_id", "user__email", "ip", "user_agent")
    ordering = ("-created_at", "-id")
    readonly_fields = (
        "device_id",
        "user",
        "created_at",
        "expires_at",
        "last_used_at",
        "revoked_at",
        "user_agent",
        "ip",
    )
    exclude = ("secret_hash",)
    actions = ("revoke_selected",)

    def has_add_permission(self, request) -> bool:
        # A device is only ever minted by a successful login that asked for it.
        return False

    @admin.action(description=_("Revoke selected devices"))
    def revoke_selected(self, request, queryset) -> None:
        updated = queryset.filter(revoked_at__isnull=True).update(
            revoked_at=timezone.now()
        )
        self.message_user(request, _("Revoked %(count)d device(s).") % {"count": updated})
