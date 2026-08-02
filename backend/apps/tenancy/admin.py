"""Django admin for tenancy.

``Membership`` and ``AuditLog`` set ``default_manager_name = "objects"`` on a
manager that **raises** ``OrgScopeError`` when a queryset is evaluated without
``.for_org()`` / ``.for_user()``. That is the multi-tenant safety net, and
Django admin is one of its four documented escape hatches. Every read below
therefore goes through the unguarded ``all_objects`` manager — via
:class:`~apps.common.admin_support.UnguardedQuerySetMixin`, so the exemption is
declared once, by name, instead of being re-derived in each ``get_queryset``.

**Do not "simplify" that away.** Dropping the mixin does not produce a subtle
leak, it produces a hard 500 on every changelist — but the same is true of the
subtler variants: ``org.memberships.all()`` and ``user.memberships.all()``
inherit the guard too, because Django builds reverse related managers from
``Model._default_manager.__class__``. Any column that walks a reverse relation
here must go through ``all_objects`` or a ``Prefetch`` carrying an
``all_objects`` queryset.

``Organization`` is the scope root and is *not* guarded, so it uses plain
``ModelAdmin`` machinery.
"""

from __future__ import annotations

from django.contrib import admin
from django.db.models import Count, Prefetch
from django.utils.translation import gettext_lazy as _

from apps.common.admin_support import (
    EMPTY,
    AdminNoteMixin,
    LargeTableAdminMixin,
    ReadOnlyAdminMixin,
    UnguardedQuerySetMixin,
    distinct_values_filter,
    pretty_json,
)
from apps.common.admin_integrations import EvidenceExportAdminMixin, ModelResource
from apps.tenancy.models import AuditLog, Membership, Organization, Role

#: ``to_attr`` for the owner prefetch on the Organization changelist. A plain
#: list, so nothing ever touches the guarded reverse manager.
OWNERS_CACHE_ATTR = "_admin_owner_memberships"


class AuditLogResource(ModelResource):
    """Column list for the CSV/JSON export of the audit trail.

    ``payload`` is included and is the reason this is worth stating: it is a
    ``JSONField``, and CSV flattens it to whatever ``str()`` produces. That is
    ugly but not lossy, and the alternative — dropping it — would export the
    fact that something happened while discarding what it was. The values in
    it were already scrubbed of secret-looking keys at *write* time, so this
    exports the same redacted text the change form shows; nothing is being
    revealed here that the admin does not already display.

    ``org`` and ``actor`` are exported by their human identifiers for the same
    reason as ``ConsentRecordResource.user__email``, and cost nothing extra:
    the changelist's ``list_select_related`` covers both.
    """

    class Meta:
        model = AuditLog
        fields = (
            "id",
            "created_at",
            "action",
            "org__slug",
            "actor__email",
            "target_type",
            "target_id",
            "payload",
            "ip",
            "user_agent",
        )
        export_order = fields


class MembershipInline(UnguardedQuerySetMixin, admin.TabularInline):
    """Who is in this workspace — editable in place.

    This is the only place a membership is created or a role changed by hand,
    which is why it is an inline on Organization rather than a bare link to the
    Membership changelist: a role only means something next to the workspace it
    is a role *in*.
    """

    model = Membership
    extra = 0
    fields = ("user", "role", "created_at", "updated_at")
    readonly_fields = ("created_at", "updated_at")
    ordering = ("created_at", "id")
    show_change_link = True
    # raw_id, not autocomplete: `autocomplete_fields` requires the *related*
    # ModelAdmin to declare `search_fields`, and the User admin belongs to
    # apps.accounts. A raw id widget has no cross-app requirement, so tenancy's
    # admin checks stay green no matter how accounts registers its User — and
    # the raw id popup is itself a search UI, so it scales with the user table.
    raw_id_fields = ("user",)

    def get_queryset(self, request):
        # The mixin supplies the unguarded manager; `select_related` here keeps
        # the inline at one query instead of one per member.
        queryset = super().get_queryset(request).select_related("user", "org")
        # Restated deliberately. `InlineModelAdmin.get_queryset` — which the
        # mixin replaces wholesale — hides the rows from a staff user holding
        # neither view nor change permission on Membership. The mixin is a
        # faithful copy of *ModelAdmin*'s version, which has no such rule
        # (the page it belongs to is already gated), so an inline has to put it
        # back or the org page leaks its member list to any staff account.
        if not self.has_view_or_change_permission(request):
            queryset = queryset.none()
        return queryset


@admin.register(Organization)
class OrganizationAdmin(AdminNoteMixin, admin.ModelAdmin):
    """A workspace. Every piece of tenant data hangs off exactly one of these."""

    admin_note = _(
        "plan and cycle are the denormalised copy that lets tenancy stay free of "
        "any billing dependency. When Subscription ships it becomes the source "
        "of truth and editing these here stops being meaningful."
    )

    list_display = (
        "name",
        "slug",
        "plan",
        "cycle",
        "member_count",
        "owners",
        "created_at",
    )
    list_display_links = ("name",)
    list_filter = ("plan", "cycle", "created_at")
    # The reverse join is what makes "which workspace does this address belong
    # to?" answerable from one box. Django adds `.distinct()` by itself for a
    # search that spans a multi-valued relation.
    search_fields = ("name", "slug", "memberships__user__email")
    ordering = ("name", "id")
    date_hierarchy = "created_at"
    list_per_page = 50
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at", "member_count", "owners")
    inlines = [MembershipInline]
    fieldsets = (
        (
            _("Identity"),
            {
                "fields": ("name", "slug"),
                "description": _(
                    "The slug is unique and appears in URLs. Changing it after a "
                    "workspace is in use breaks any link that carries it."
                ),
            },
        ),
        (_("Billing (denormalised)"), {"fields": ("plan", "cycle")}),
        (_("Members"), {"fields": ("member_count", "owners")}),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )

    def get_queryset(self, request):
        """One query for the member count, one for the owners — not N of each.

        ``Count`` over the reverse relation is compiled into a JOIN by the ORM
        and never touches ``Membership.objects``, so the org guard is not
        involved. ``distinct=True`` is load-bearing: ``search_fields`` spans the
        same relation, and without it a search would multiply the count by the
        number of matching members.
        """
        queryset = super().get_queryset(request)
        return queryset.annotate(
            _member_count=Count("memberships", distinct=True)
        ).prefetch_related(
            Prefetch(
                "memberships",
                queryset=Membership.all_objects.select_related("user").filter(
                    role=Role.OWNER
                ),
                to_attr=OWNERS_CACHE_ATTR,
            )
        )

    def _may_see_members(self, request) -> bool:
        """Same gate the Membership inline applies, for the computed columns.

        The inline restores Django's view/change check so the org page cannot
        leak its member list to a staff account without Membership permission.
        ``member_count`` and ``owners`` read the same table through the same
        unguarded manager and are rendered on the same two pages, so without
        this they hand back the member count and every owner's e-mail address
        to exactly the account the inline was written to keep out.
        """
        user = request.user
        return user.has_perm("tenancy.view_membership") or user.has_perm(
            "tenancy.change_membership"
        )

    #: Columns that read Membership and therefore need Membership permission.
    MEMBER_COLUMNS = ("member_count", "owners")

    def get_list_display(self, request):
        """Drop the member columns rather than blanking them per row.

        Deciding inside the column callable would need the request, and a
        ``self._request`` stash is not thread-safe: Django instantiates one
        ModelAdmin per registration and shares it across worker threads, so
        two concurrent staff requests would race. Filtering the column list —
        which Django already calls per request — has no such problem.
        """
        columns = super().get_list_display(request)
        if self._may_see_members(request):
            return columns
        return tuple(c for c in columns if c not in self.MEMBER_COLUMNS)

    def get_fieldsets(self, request, obj=None):
        fieldsets = super().get_fieldsets(request, obj)
        if self._may_see_members(request):
            return fieldsets
        return tuple(
            (name, {**opts, "fields": tuple(
                f for f in opts.get("fields", ()) if f not in self.MEMBER_COLUMNS
            )})
            for name, opts in fieldsets
            if tuple(f for f in opts.get("fields", ()) if f not in self.MEMBER_COLUMNS)
        )

    def get_inlines(self, request, obj=None):
        # The inline already blanks its own queryset without permission; hiding
        # the empty box as well keeps the page honest rather than suggesting
        # the workspace has no members.
        if self._may_see_members(request):
            return super().get_inlines(request, obj)
        return ()

    @admin.display(description=_("members"), ordering="_member_count")
    def member_count(self, obj: Organization):
        count = getattr(obj, "_member_count", None)
        if count is None:
            # The change form does not go through the annotated changelist
            # queryset in every code path; `all_objects` keeps the guard happy.
            count = Membership.all_objects.filter(org=obj).count()
        return count

    @admin.display(description=_("owners"))
    def owners(self, obj: Organization) -> str:
        rows = getattr(obj, OWNERS_CACHE_ATTR, None)
        if rows is None:
            if obj.pk is None:
                return EMPTY
            rows = list(
                Membership.all_objects.filter(org=obj, role=Role.OWNER).select_related(
                    "user"
                )
            )
        if not rows:
            return EMPTY
        return ", ".join(row.user.email for row in rows)


@admin.register(Membership)
class MembershipAdmin(UnguardedQuerySetMixin, AdminNoteMixin, admin.ModelAdmin):
    """A user's role in a workspace.

    Usually reached through the Organization inline; this changelist exists for
    the other direction — "every workspace this person is in", and "every owner
    on the free plan".
    """

    admin_note = _(
        "There is no bulk role action on purpose: demoting the last owner of a "
        "workspace leaves it unadministrable, and no selection carries enough "
        "context to tell whether that is what happened."
    )

    list_display = ("id", "org", "user", "role", "org_plan", "created_at")
    list_display_links = ("id", "org")
    list_filter = ("role", "org__plan", "created_at")
    search_fields = ("org__name", "org__slug", "user__email")
    ordering = ("-created_at", "-id")
    date_hierarchy = "created_at"
    list_per_page = 50
    list_select_related = ("org", "user")
    # `org` is same-app and OrganizationAdmin declares search_fields, so
    # autocomplete works and is the better widget. `user` stays raw_id for the
    # cross-app reason spelled out on MembershipInline.
    autocomplete_fields = ("org",)
    raw_id_fields = ("user",)
    readonly_fields = ("created_at", "updated_at")
    fieldsets = (
        (_("Link"), {"fields": ("org", "user")}),
        (
            _("Role"),
            {
                "fields": ("role",),
                "description": _(
                    "owner and admin may both change workspace settings; only "
                    "owner is meant to be irreducible. One membership per "
                    "(workspace, user) is enforced by the database."
                ),
            },
        ),
        (_("Timestamps"), {"fields": ("created_at", "updated_at")}),
    )

    @admin.display(description=_("plan"), ordering="org__plan")
    def org_plan(self, obj: Membership) -> str:
        return obj.org.get_plan_display()


@admin.register(AuditLog)
class AuditLogAdmin(
    EvidenceExportAdminMixin,
    LargeTableAdminMixin,
    ReadOnlyAdminMixin,
    UnguardedQuerySetMixin,
    admin.ModelAdmin,
):
    """Append-only record of security- and tenancy-relevant events.

    The model refuses an UPDATE, so admin offers no form. Deletion is off here
    too — an audit trail a staff user can prune is not an audit trail. (Cascade
    from a deleted Organization still works; that is the ORM, not this admin.)

    This is the fastest-growing table in the project — every login, every role
    change, every workspace event — which is what ``LargeTableAdminMixin`` is
    for. Note the mixin order: ``EvidenceExportAdminMixin`` exports through
    ``get_queryset``, so it has to sit above ``UnguardedQuerySetMixin`` and
    inherit the unguarded manager rather than reaching for ``AuditLog.objects``,
    which raises ``OrgScopeError``.
    """

    admin_note = _(
        "Append-only. Payloads are scrubbed of secret-looking keys before they "
        "are written — a value showing as [redacted] was redacted at write time "
        "and the original was never stored. Select rows and use the export "
        "action for an incident write-up. Every completed export appends an "
        "admin.export row to this same table."
    )

    resource_classes = [AuditLogResource]

    list_display = ("created_at", "action", "org", "actor", "target", "ip")
    list_display_links = ("created_at", "action")
    # Bounded distinct-value filters: `action` and `target_type` are free-text
    # columns whose vocabulary grows with the code base, and Django's default
    # AllValuesFieldListFilter would run an unbounded SELECT DISTINCT on every
    # page render. `org` and `actor` are deliberately NOT filters — a related
    # field filter renders one <option> per row in the related table.
    list_filter = (
        distinct_values_filter("action", _("action")),
        distinct_values_filter("target_type", _("object type")),
        "created_at",
    )
    search_fields = (
        "action",
        "target_type",
        "target_id",
        "actor__email",
        "org__name",
        "org__slug",
    )
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    # `list_per_page` comes from LargeTableAdminMixin.
    list_select_related = ("org", "actor")
    readonly_fields = (
        "created_at",
        "action",
        "org",
        "actor",
        "target_type",
        "target_id",
        "payload_pretty",
        "ip",
        "user_agent",
    )
    fieldsets = (
        (_("Event"), {"fields": ("created_at", "action")}),
        (_("Scope"), {"fields": ("org", "actor")}),
        (_("Target"), {"fields": ("target_type", "target_id")}),
        (
            _("Payload"),
            {
                "fields": ("payload_pretty",),
                "description": _(
                    "Recursively scrubbed at write time: any key that looks like "
                    "a code, password, secret, token or API key is stored as "
                    "[redacted]."
                ),
            },
        ),
        (_("Request metadata"), {"fields": ("ip", "user_agent")}),
    )

    @admin.display(description=_("target"))
    def target(self, obj: AuditLog) -> str:
        if not obj.target_type and not obj.target_id:
            return EMPTY
        if not obj.target_id:
            return obj.target_type
        return f"{obj.target_type} #{obj.target_id}"

    @admin.display(description=_("payload"))
    def payload_pretty(self, obj: AuditLog):
        return pretty_json(obj.payload)
