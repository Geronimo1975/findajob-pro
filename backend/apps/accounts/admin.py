"""Django admin for accounts.

Three ModelAdmins, all of them defensive about one thing: **nothing here may
make a login code, or a password, readable, guessable or forgeable.**

* ``EmailOTP.code_hash`` is never rendered. It is an HMAC over a six-digit
  preimage, so anyone holding it *and* ``SECRET_KEY`` can enumerate the code in
  under a second. The changelist shows a derived
  :func:`~apps.common.admin_support.short_digest` instead — enough to tell two
  rows apart, never the stored value itself. It is also kept out of
  ``search_fields``: a searchable hash column is a verification oracle.
* ``EmailOTP`` rows cannot be added or edited from admin — a staff user who
  could write ``code_hash`` could mint themselves a working code for any
  address. Deleting is allowed (that is just an early expiry) and is the
  mechanism behind the *purge* action.
* ``User`` uses plain ``ModelAdmin``, not ``django.contrib.auth.admin.UserAdmin``:
  that class is built around a ``username`` field this model does not have. The
  password **hash is not rendered at all** — not even read-only, which is what
  Django's ``ReadOnlyPasswordHashField`` would do. The change form shows only
  whether a usable password exists, because that is the operationally useful
  fact ("is this an admin account or an OTP-only account?") and the hash itself
  is not.

Cross-app note
--------------
Spec §2 forbids ``apps.accounts`` from importing ``Organization`` /
``Membership`` / ``AuditLog``. The *workspaces* column needs Membership, so it
resolves the model through the app registry at call time — the same technique
``apps.tenancy.managers`` already uses to avoid an import cycle. There is no
module-level dependency, and accounts still boots with ``tenancy`` absent: the
column degrades to an em dash.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.contrib import admin, messages
from django.db.models import Prefetch, Q
from django.utils import timezone
from django.utils.timesince import timesince
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from apps.accounts.models import (
    MAX_OTP_ATTEMPTS,
    EmailOTP,
    SuppressedAddress,
    User,
)
from apps.common.admin_support import (
    EMPTY,
    AdminNoteMixin,
    short_digest,
)

#: Attribute the User changelist prefetches memberships into. ``to_attr`` keeps
#: the result a plain list, which matters: ``user.memberships`` is a reverse
#: related manager built from ``Membership._default_manager.__class__``, i.e.
#: the org-scoped one, and evaluating it raises ``OrgScopeError``.
MEMBERSHIP_CACHE_ATTR = "_admin_memberships"


def _membership_model():
    """``tenancy.Membership``, or ``None`` when tenancy is not installed."""
    try:
        return django_apps.get_model("tenancy", "Membership")
    except LookupError:  # pragma: no cover - tenancy is installed in this project
        return None


@admin.register(User)
class UserAdmin(AdminNoteMixin, admin.ModelAdmin):
    """The account. Everything an operator needs to answer "who is this?"."""

    admin_note = _(
        "Passwords are never shown or set here. A new account created from this "
        "page gets an unusable password, i.e. it can only log in with an email "
        "code; give an admin account its password with "
        "`manage.py changepassword`."
    )

    list_display = (
        "email",
        "is_verified",
        "is_active",
        "is_staff",
        "is_superuser",
        "lang",
        "workspaces",
        "date_joined",
    )
    list_display_links = ("email",)
    list_filter = (
        "is_verified",
        "is_active",
        "is_staff",
        "is_superuser",
        "lang",
        "date_joined",
    )
    # Also required by any other app that wants `autocomplete_fields = ("user",)`.
    search_fields = ("email",)
    # Newest first: the operator's question is almost always about a recent
    # signup, and `Meta.ordering` (email) answers a question nobody asks.
    ordering = ("-date_joined", "-id")
    date_hierarchy = "date_joined"
    list_per_page = 50
    filter_horizontal = ("groups", "user_permissions")
    readonly_fields = (
        "date_joined",
        "updated_at",
        "last_login",
        "password_state",
        "workspaces",
    )
    actions = ("deactivate_users", "activate_users")

    #: Columns that must never be reachable through a changelist query string.
    #: ``ModelAdmin.lookup_allowed`` returns True for anything nameable in
    #: ``list_filter``/``search_fields``' relation graph, so keeping ``password``
    #: out of the rendered form and ``code_hash`` out of ``search_fields`` is not
    #: enough: ``?password__startswith=pbkdf2_sha256%24`` turns the changelist
    #: into a one-bit oracle you can bisect a hash with.
    FORBIDDEN_LOOKUP_ROOTS = frozenset({"password"})

    def lookup_allowed(self, lookup, value, request=None):
        if lookup.split("__", 1)[0] in self.FORBIDDEN_LOOKUP_ROOTS:
            return False
        return super().lookup_allowed(lookup, value, request)

    def get_readonly_fields(self, request, obj=None):
        """Staff may not escalate themselves, and may not edit a superuser.

        The Permissions fieldset is editable so a superuser can grant staff
        access from the UI. Left unguarded it is a privilege-escalation
        primitive: any account holding ``accounts.change_user`` could POST
        ``is_superuser=on`` to its own change form.
        """
        readonly = tuple(super().get_readonly_fields(request, obj))
        if not request.user.is_superuser:
            readonly += ("is_superuser", "is_staff", "groups", "user_permissions")
        return readonly

    def has_change_permission(self, request, obj=None):
        if obj is not None and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj is not None and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)

    fieldsets = (
        (None, {"fields": ("email", "lang", "is_verified")}),
        (
            _("Workspaces"),
            {
                "fields": ("workspaces",),
                "description": _(
                    "Memberships are managed from Tenancy → Organizations."
                ),
            },
        ),
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
        (
            _("Credentials"),
            {
                "fields": ("password_state",),
                "classes": ("collapse",),
                "description": _(
                    "The password hash is deliberately not rendered. Login is a "
                    "6-digit email code; a password exists only on accounts that "
                    "sign in to this admin."
                ),
            },
        ),
    )

    # -- queryset ---------------------------------------------------------
    # No FK on User, so `list_select_related` has nothing to do. The one
    # relational column is the reverse `memberships`, which needs a Prefetch
    # against the UNGUARDED manager (see MEMBERSHIP_CACHE_ATTR).

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        membership = _membership_model()
        if membership is None:  # pragma: no cover - tenancy is installed
            return queryset
        return queryset.prefetch_related(
            Prefetch(
                "memberships",
                queryset=membership.all_objects.select_related("org"),
                to_attr=MEMBERSHIP_CACHE_ATTR,
            )
        )

    # -- computed columns -------------------------------------------------

    @admin.display(description=_("workspaces"))
    def workspaces(self, obj: User) -> str:
        rows = getattr(obj, MEMBERSHIP_CACHE_ATTR, None)
        if rows is None:
            membership = _membership_model()
            if membership is None or obj.pk is None:
                return EMPTY
            rows = list(membership.all_objects.filter(user=obj).select_related("org"))
        if not rows:
            return EMPTY
        return ", ".join(f"{row.org.name} ({row.role})" for row in rows)

    @admin.display(description=_("password"))
    def password_state(self, obj: User) -> str:
        """Whether a usable password exists — never the hash itself."""
        if obj.pk is None:
            return _("not set — this account will be email-code only")
        if obj.has_usable_password():
            return _("set — this account can sign in to Django admin")
        return _("unusable — email-code login only")

    # -- writes -----------------------------------------------------------

    def save_model(self, request, obj: User, form, change) -> None:
        """A user created here gets an *unusable* password, never a blank one.

        ``password`` is not on the form, so a fresh row would otherwise be saved
        with ``""`` — which is not the same thing as unusable and leaves the
        account in a state no code path expects.
        """
        if not change and not obj.password:
            obj.set_unusable_password()
        super().save_model(request, obj, form, change)

    # -- actions ----------------------------------------------------------
    # Both are reversible by the other, and both refuse to touch a superuser or
    # the person clicking the button — a bulk action must not be able to lock
    # the last administrator out of the box.

    @admin.action(
        description=_("Deactivate selected users (cannot sign in)"),
        permissions=["change"],
    )
    def deactivate_users(self, request, queryset):
        self._set_active(request, queryset, active=False)

    @admin.action(description=_("Reactivate selected users"), permissions=["change"])
    def activate_users(self, request, queryset):
        self._set_active(request, queryset, active=True)

    def _set_active(self, request, queryset, *, active: bool) -> None:
        protected = queryset.filter(Q(is_superuser=True) | Q(pk=request.user.pk))
        skipped = protected.count()
        # `.update()` does NOT fire auto_now, so `updated_at` is written by hand
        # or the row silently keeps a stale timestamp.
        changed = queryset.exclude(
            Q(is_superuser=True) | Q(pk=request.user.pk)
        ).update(is_active=active, updated_at=timezone.now())

        verb = _("reactivated") if active else _("deactivated")
        self.message_user(
            request,
            ngettext("%(count)d user %(verb)s.", "%(count)d users %(verb)s.", changed)
            % {"count": changed, "verb": verb},
            messages.SUCCESS if changed else messages.WARNING,
        )
        if skipped:
            self.message_user(
                request,
                ngettext(
                    "%(count)d row skipped: superusers and your own account are "
                    "never changed in bulk.",
                    "%(count)d rows skipped: superusers and your own account are "
                    "never changed in bulk.",
                    skipped,
                )
                % {"count": skipped},
                messages.WARNING,
            )


class OTPStateFilter(admin.SimpleListFilter):
    """Filter on the computed lifecycle state.

    The four buckets are mutually exclusive and evaluated in the same order as
    :meth:`EmailOTPAdmin.state`, so what the sidebar selects is always what the
    column says. They are expressed as ORM predicates rather than derived in
    Python because the changelist must stay a single query.
    """

    title = _("state")
    parameter_name = "state"

    LIVE = "live"
    USED = "used"
    EXPIRED = "expired"
    BURNT = "burnt"

    def lookups(self, request, model_admin):
        return (
            (self.LIVE, _("live — still verifiable")),
            (self.USED, _("used — consumed by a successful login")),
            (self.EXPIRED, _("expired — past its 10-minute window")),
            (self.BURNT, _("burnt — %(n)d wrong guesses") % {"n": MAX_OTP_ATTEMPTS}),
        )

    def queryset(self, request, queryset):
        value = self.value()
        if value is None:
            return queryset
        now = timezone.now()
        if value == self.USED:
            return queryset.filter(consumed_at__isnull=False)
        unconsumed = queryset.filter(consumed_at__isnull=True)
        if value == self.BURNT:
            return unconsumed.filter(attempts__gte=MAX_OTP_ATTEMPTS)
        if value == self.EXPIRED:
            return unconsumed.filter(
                attempts__lt=MAX_OTP_ATTEMPTS, expires_at__lte=now
            )
        if value == self.LIVE:
            return unconsumed.filter(attempts__lt=MAX_OTP_ATTEMPTS, expires_at__gt=now)
        return queryset


@admin.register(EmailOTP)
class EmailOTPAdmin(AdminNoteMixin, admin.ModelAdmin):
    """Read-only + delete. Answers "did the code go out, and what happened to it?"

    Add and change are off: a staff user who could write ``code_hash`` could
    mint a working code for any address, which is a complete authentication
    bypass. Delete stays on — deleting a row is indistinguishable from the code
    expiring, and it is the mechanism behind :meth:`purge_expired`.
    """

    #: ``code_hash`` is deliberately absent from ``search_fields`` so the
    #: changelist cannot be used to confirm a guessed hash. That is defeated by
    #: ``?code_hash__startswith=…`` unless the lookup itself is refused: the
    #: changelist would answer "row exists / does not exist" one hex character
    #: at a time, which recovers the hash without ever reading the column.
    FORBIDDEN_LOOKUP_ROOTS = frozenset({"code_hash"})

    def lookup_allowed(self, lookup, value, request=None):
        if lookup.split("__", 1)[0] in self.FORBIDDEN_LOOKUP_ROOTS:
            return False
        return super().lookup_allowed(lookup, value, request)

    admin_note = _(
        "Login codes are never shown — not even hashed. This table is read-only; "
        "rows can only be deleted, which is the same thing as expiring them early."
    )

    list_display = (
        "email",
        "state",
        "purpose",
        "created_at",
        "expires_at",
        "attempts",
        "consumed_at",
        "request_ip",
        "fingerprint",
    )
    list_display_links = ("email",)
    list_filter = (
        OTPStateFilter,
        "purpose",
        ("consumed_at", admin.EmptyFieldListFilter),
        ("request_ip", admin.EmptyFieldListFilter),
    )
    # `code_hash` is deliberately absent: a searchable hash is an oracle that
    # confirms a guess without ever showing the value.
    search_fields = ("email", "request_ip")
    date_hierarchy = "created_at"
    ordering = ("-created_at", "-id")
    list_per_page = 50
    actions = ("purge_expired",)
    # No FK on this model at all — deliberately, so that `request-code` cannot
    # look a user up and is therefore branch-free (spec §6). Nothing to
    # `list_select_related`.
    fieldsets = (
        (
            _("Recipient"),
            {
                "fields": ("email", "purpose", "fingerprint"),
                "description": _(
                    "The fingerprint is a digest of the stored hash, not the hash "
                    "itself. It identifies a row across screens and logs; it is "
                    "not the code and cannot be turned back into one here."
                ),
            },
        ),
        (
            _("Lifecycle"),
            {"fields": ("state", "created_at", "expires_at", "consumed_at")},
        ),
        (_("Abuse signals"), {"fields": ("attempts", "request_ip")}),
    )
    readonly_fields = (
        "email",
        "purpose",
        "fingerprint",
        "state",
        "created_at",
        "expires_at",
        "consumed_at",
        "attempts",
        "request_ip",
    )

    # -- computed columns -------------------------------------------------

    @admin.display(description=_("state"))
    def state(self, obj: EmailOTP) -> str:
        # Order matters and mirrors OTPStateFilter: a consumed row that is also
        # past `expires_at` is "used", not "expired".
        if obj.is_consumed():
            return _("used")
        if obj.is_exhausted():
            return _("burnt")
        if obj.is_expired():
            return _("expired")
        return _("live")

    @admin.display(description=_("fingerprint"))
    def fingerprint(self, obj: EmailOTP) -> str:
        return short_digest(obj.code_hash) or EMPTY

    # -- permissions ------------------------------------------------------

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    # -- actions ----------------------------------------------------------

    @admin.action(
        description=_("Purge selected codes that are used, expired or burnt"),
        permissions=["delete"],
    )
    def purge_expired(self, request, queryset):
        """Delete every *dead* row in the selection; never a live one.

        Filtering inside the action rather than trusting the selection is the
        whole point: "select all", then purge, is the intended operator gesture,
        and it must not be able to delete a code somebody is mid-way through
        typing.
        """
        now = timezone.now()
        dead = queryset.filter(
            Q(consumed_at__isnull=False)
            | Q(expires_at__lte=now)
            | Q(attempts__gte=MAX_OTP_ATTEMPTS)
        )
        purged = dead.count()
        kept = queryset.count() - purged
        dead.delete()

        self.message_user(
            request,
            ngettext(
                "%(count)d dead code purged.",
                "%(count)d dead codes purged.",
                purged,
            )
            % {"count": purged},
            messages.SUCCESS if purged else messages.WARNING,
        )
        if kept:
            self.message_user(
                request,
                ngettext(
                    "%(count)d live code kept — a code that can still be typed is "
                    "never purged.",
                    "%(count)d live codes kept — a code that can still be typed is "
                    "never purged.",
                    kept,
                )
                % {"count": kept},
                messages.INFO,
            )


@admin.register(SuppressedAddress)
class SuppressedAddressAdmin(AdminNoteMixin, admin.ModelAdmin):
    """The one place a wrongly-suppressed address can be released.

    Deleting a row here is the supported un-suppress: the next
    ``request-code`` for that address will mail again. That is why
    :mod:`apps.accounts.webhooks` errs toward *not* suppressing — a false
    positive is a login lockout only a human can clear.

    Add is off because every row is written by the signature-verified Brevo
    webhook (or by ``SuppressedAddress.suppress()``); a hand-typed address here
    would be a login lockout created by a typo. Change stays on so a row can be
    re-classified — that is how ``manual`` is reachable.
    """

    admin_note = _(
        "Written by the Brevo bounce/complaint webhook. Deleting a row is the "
        "supported way to un-suppress an address: the next login request for it "
        "will be mailed again."
    )

    list_display = ("email", "reason", "suppressed_for", "created_at", "updated_at")
    list_display_links = ("email",)
    list_filter = ("reason", "created_at")
    search_fields = ("email",)
    ordering = ("-created_at", "-id")
    date_hierarchy = "created_at"
    list_per_page = 50
    # No FK: a suppression is about an address, not an account — spec §6 keeps
    # the mail path branch-free, so it must work for addresses with no user.
    # Nothing to `list_select_related`.
    # ``email`` is read-only for the same reason ``has_add_permission`` is False:
    # a hand-typed or mistyped address here is a login lockout for a real user,
    # and it is silent — the OTP endpoint still answers 202. Re-classification
    # only needs ``reason`` to be writable; un-suppressing is deleting the row.
    readonly_fields = ("email", "created_at", "updated_at", "suppressed_for")
    fieldsets = (
        (_("Address"), {"fields": ("email",)}),
        (
            _("Classification"),
            {
                "fields": ("reason",),
                "description": _(
                    "Re-classifying does not change behaviour — any row at all "
                    "stops mail to this address. It records why."
                ),
            },
        ),
        (
            _("Timestamps"),
            {"fields": ("suppressed_for", "created_at", "updated_at")},
        ),
    )

    @admin.display(description=_("suppressed for"))
    def suppressed_for(self, obj: SuppressedAddress) -> str:
        if obj.created_at is None:
            return EMPTY
        return timesince(obj.created_at, timezone.now())

    def has_add_permission(self, request) -> bool:
        return False
