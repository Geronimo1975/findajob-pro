from django.contrib import admin

from apps.outbox.models import MailboxConnection


@admin.register(MailboxConnection)
class MailboxConnectionAdmin(admin.ModelAdmin):
    list_display = (
        "email_address", "provider", "org", "user", "status", "last_verified_at"
    )
    list_filter = ("provider", "status")
    search_fields = ("email_address", "user__email")
    # Secrets stay write-only even for staff.
    exclude = ("credentials_encrypted",)
    readonly_fields = ("status", "last_verified_at", "last_error_code", "created_at")

    def get_queryset(self, request):
        return MailboxConnection.all_objects.all()
