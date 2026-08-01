"""Django admin for tenancy.

Every ModelAdmin here calls ``.unscoped()`` explicitly: the guarded managers
raise on an unscoped read by design, and staff-only admin is one of the four
legitimate escape hatches (admin, migrations, management commands, tests).
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

from apps.tenancy.models import AuditLog, Membership, Organization


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    fields = ("user", "role", "created_at")
    readonly_fields = ("created_at",)
    # raw_id, not autocomplete: `autocomplete_fields` requires the *related*
    # ModelAdmin to declare `search_fields`, and the User admin belongs to
    # apps.accounts. A raw id widget has no cross-app requirement, so tenancy's
    # admin checks stay green no matter how accounts registers its User.
    raw_id_fields = ("user",)

    def get_queryset(self, request):
        # Bypasses the org guard; the inline is already filtered to one org by
        # the formset.
        return Membership.all_objects.select_related("user")


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "plan", "cycle", "member_count", "created_at")
    list_filter = ("plan", "cycle")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at")
    ordering = ("name",)
    inlines = [MembershipInline]

    @admin.display(description=_("members"))
    def member_count(self, obj) -> int:
        return Membership.all_objects.filter(org=obj).count()


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("id", "org", "user", "role", "created_at")
    list_filter = ("role",)
    search_fields = ("org__name", "org__slug")
    autocomplete_fields = ("org",)
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")

    def get_queryset(self, request):
        return Membership.all_objects.select_related("org", "user")


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Read-only in the UI — the model itself refuses updates."""

    list_display = ("created_at", "action", "org", "actor", "target_type", "target_id", "ip")
    list_filter = ("action",)
    search_fields = ("action", "target_type", "target_id")
    date_hierarchy = "created_at"
    readonly_fields = (
        "org",
        "actor",
        "action",
        "target_type",
        "target_id",
        "payload",
        "ip",
        "user_agent",
        "created_at",
    )

    def get_queryset(self, request):
        return AuditLog.all_objects.select_related("org", "actor")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
