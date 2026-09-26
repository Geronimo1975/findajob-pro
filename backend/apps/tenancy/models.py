"""Tenancy models — Organization, Membership, AuditLog.

Implementation spec §2 pins ownership: these three live here and **only** here.
``apps.accounts`` may not define, import or instantiate them beyond calling
``apps.tenancy.services.bootstrap_workspace()``.

Nothing in this module may reference ``Invitation`` (build-order position 7),
``Subscription`` or ``UsageQuota`` (positions 5–6). ``plan`` and ``cycle`` are
plain fields on ``Organization`` exactly so that tenancy has no dependency on
billing; when ``Subscription`` ships it becomes the source of truth and these
stay as the denormalised, cheap-to-read copy.

Django 6.0: ``CheckConstraint`` takes ``condition=`` — ``check=`` was removed.
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.tenancy.managers import OrganizationManager, OrgScopedManager

MAX_ORG_NAME_LENGTH = 120
MAX_ORG_SLUG_LENGTH = 140


class Plan(models.TextChoices):
    """Mirrors ``PLANS`` in ``web/api.js``. Free / Pro / Max / Team."""

    FREE = "free", _("Free")
    PRO = "pro", _("Pro")
    MAX = "max", _("Max")
    TEAM = "team", _("Team")


class BillingCycle(models.TextChoices):
    MONTHLY = "monthly", _("Monthly")
    YEARLY = "yearly", _("Yearly")


class Role(models.TextChoices):
    """Workspace roles, most privileged first."""

    OWNER = "owner", _("Owner")
    ADMIN = "admin", _("Admin")
    MEMBER = "member", _("Member")


#: Roles allowed to change workspace-wide settings.
ADMIN_ROLES = frozenset({Role.OWNER.value, Role.ADMIN.value})


class Organization(models.Model):
    """A workspace. Every piece of tenant data hangs off exactly one of these."""

    name = models.CharField(_("name"), max_length=MAX_ORG_NAME_LENGTH)
    slug = models.SlugField(_("slug"), max_length=MAX_ORG_SLUG_LENGTH, unique=True)
    plan = models.CharField(
        _("plan"), max_length=16, choices=Plan.choices, default=Plan.FREE
    )
    cycle = models.CharField(
        _("billing cycle"),
        max_length=16,
        choices=BillingCycle.choices,
        default=BillingCycle.MONTHLY,
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = OrganizationManager()

    class Meta:
        verbose_name = _("organization")
        verbose_name_plural = _("organizations")
        ordering = ("name", "id")
        constraints = [
            # Django 6.0: `condition=`, not `check=` (removed).
            # Deliberately not a regex: SQLite cannot evaluate Django's
            # user-registered REGEXP function inside a CHECK constraint, so a
            # regex here would break `migrate` on the test database.
            models.CheckConstraint(
                condition=~models.Q(name=""),
                name="organization_name_not_blank",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    @property
    def members_count(self) -> int:
        return Membership.objects.for_org(self).count()


class Membership(models.Model):
    """Links a user to an organization with a role.

    A user may belong to several organizations; at MVP
    :func:`apps.tenancy.services.bootstrap_workspace` gives every new user
    exactly one, owned by them.
    """

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name=_("organization"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
        verbose_name=_("user"),
    )
    role = models.CharField(
        _("role"), max_length=16, choices=Role.choices, default=Role.MEMBER
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    #: Guarded: reads must go through ``.for_org()`` / ``.for_user()``.
    objects = OrgScopedManager(user_field="user")
    #: Unguarded, for Django admin and management commands only.
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        ordering = ("created_at", "id")
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        constraints = [
            models.UniqueConstraint(
                fields=("org", "user"), name="uniq_membership_per_org_user"
            ),
            models.CheckConstraint(
                condition=models.Q(role__in=[choice.value for choice in Role]),
                name="membership_role_valid",
            ),
        ]
        indexes = [
            models.Index(fields=["user", "org"], name="membership_user_org_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.user_id} @ {self.org_id} ({self.role})"

    @property
    def is_admin(self) -> bool:
        return self.role in ADMIN_ROLES

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

#: Keys whose values are never allowed into an audit payload. Spec §4: the OTP
#: code is *"never stored, logged, or put in an audit payload"*.
SENSITIVE_PAYLOAD_KEYS = frozenset(
    {
        "code",
        "otp",
        "one_time_code",
        "code_hash",
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "sessionid",
        "csrftoken",
    }
)

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(^|_)(code|otp|password|secret|token|api_?key)($|_)"
)

#: Keys that would otherwise trip ``_SENSITIVE_KEY_RE`` (they contain the word
#: "token") but hold a public, non-secret value that is already exposed
#: elsewhere (e.g. the MCP token list API returns the same prefix). Named
#: explicitly, one at a time, so the allowlist can never accidentally swallow
#: an actual secret key.
SAFE_PAYLOAD_KEYS = frozenset({"token_prefix"})

REDACTED = "[redacted]"

MAX_AUDIT_ACTION_LENGTH = 64
MAX_AUDIT_TARGET_LENGTH = 64
MAX_AUDIT_USER_AGENT_LENGTH = 400


def scrub_payload(value: Any) -> Any:
    """Recursively redact secret-looking keys in an audit payload."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text.lower() in SAFE_PAYLOAD_KEYS:
                cleaned[key_text] = item
            elif key_text.lower() in SENSITIVE_PAYLOAD_KEYS or _SENSITIVE_KEY_RE.search(
                key_text
            ):
                cleaned[key_text] = REDACTED
            else:
                cleaned[key_text] = scrub_payload(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [scrub_payload(item) for item in value]
    return value


class AuditLogManager(OrgScopedManager):
    """Org-scoped reads, plus the one blessed write helper."""

    def record(
        self,
        *,
        action: str,
        org=None,
        actor=None,
        target=None,
        target_type: str = "",
        target_id: str = "",
        payload: dict | None = None,
        ip: str | None = None,
        user_agent: str = "",
    ) -> "AuditLog":
        """Append one immutable event.

        ``target`` is a convenience: pass a model instance and its label and pk
        are derived. ``payload`` is scrubbed of secret-looking keys before it
        ever reaches the database.
        """
        if target is not None:
            target_type = target_type or target._meta.label_lower
            target_id = target_id or str(getattr(target, "pk", "") or "")
        return self.create(
            org=org,
            actor=actor,
            action=action[:MAX_AUDIT_ACTION_LENGTH],
            target_type=(target_type or "")[:MAX_AUDIT_TARGET_LENGTH],
            target_id=(target_id or "")[:MAX_AUDIT_TARGET_LENGTH],
            payload=payload or {},
            ip=ip or None,
            user_agent=(user_agent or "")[:MAX_AUDIT_USER_AGENT_LENGTH],
        )


class AuditLog(models.Model):
    """Append-only record of security- and tenancy-relevant events.

    ``org`` is nullable because some events legitimately precede a workspace
    (a login attempt, an email suppression). ``actor`` is nullable and
    ``SET_NULL`` because deleting a user must not delete the evidence.
    """

    org = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="audit_logs",
        null=True,
        blank=True,
        verbose_name=_("organization"),
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="audit_events",
        null=True,
        blank=True,
        verbose_name=_("actor"),
    )
    action = models.CharField(_("action"), max_length=MAX_AUDIT_ACTION_LENGTH)
    target_type = models.CharField(
        _("target type"), max_length=MAX_AUDIT_TARGET_LENGTH, blank=True
    )
    target_id = models.CharField(
        _("target id"), max_length=MAX_AUDIT_TARGET_LENGTH, blank=True
    )
    payload = models.JSONField(_("payload"), default=dict, blank=True)
    ip = models.GenericIPAddressField(_("ip address"), null=True, blank=True)
    user_agent = models.CharField(
        _("user agent"), max_length=MAX_AUDIT_USER_AGENT_LENGTH, blank=True
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    objects = AuditLogManager()
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("audit log entry")
        verbose_name_plural = _("audit log")
        ordering = ("-created_at", "-id")
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        indexes = [
            models.Index(fields=["org", "-created_at"], name="auditlog_org_created_idx"),
            models.Index(fields=["action", "-created_at"], name="auditlog_action_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M:%S} {self.action}"

    # Django 6.0 removed positional arguments to Model.save().
    def save(self, **kwargs) -> None:
        if self.pk is not None and not self._state.adding:
            raise ValueError(
                "AuditLog rows are append-only; write a new row instead of "
                "modifying an existing one."
            )
        self.payload = scrub_payload(self.payload or {})
        super().save(**kwargs)


def unique_org_slug(name: str, *, max_attempts: int = 50) -> str:
    """Return a slug for ``name`` that no organization is using yet.

    Best-effort only: the ``unique=True`` constraint on ``Organization.slug`` is
    the real guarantee, and :func:`apps.tenancy.services.bootstrap_workspace`
    retries on ``IntegrityError``.
    """
    base = slugify(name)[: MAX_ORG_SLUG_LENGTH - 12] or "workspace"
    candidate = base
    suffix = 1
    while Organization.objects.filter(slug=candidate).exists():
        suffix += 1
        if suffix > max_attempts:
            from secrets import token_hex

            candidate = f"{base}-{token_hex(4)}"
            break
        candidate = f"{base}-{suffix}"
    return candidate[:MAX_ORG_SLUG_LENGTH]


# Split-models module: McpToken lives in apps/tenancy/mcp/models.py but belongs
# to this app's migration history. The import must stay at the bottom so the
# mcp module can import OrgScopedManager without a cycle.
from apps.tenancy.mcp.models import McpToken  # noqa: E402,F401
