"""Tenancy services.

This module is deliberately tiny and deliberately dependency-free.

Implementation spec §2::

    bootstrap_workspace() lives in apps/tenancy.services and creates only
    Organization + Membership(role=owner). It never consults Invitation —
    invitation acceptance is a separate, token-authenticated endpoint shipped
    with org-members, because auto-accepting on login is a cross-tenant
    account-capture hole.

There is therefore **no** reference anywhere in this file to ``Invitation``,
``Subscription`` or ``UsageQuota``, and ``tests/tenancy/test_bootstrap_workspace``
asserts that by reading this module's source.
"""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from apps.common.errors import NotOrgMember
from apps.tenancy.models import (
    MAX_ORG_NAME_LENGTH,
    AuditLog,
    Membership,
    Organization,
    Role,
    unique_org_slug,
)

logger = logging.getLogger("findajob.tenancy")

#: How many slug collisions to absorb before giving up.
_SLUG_RETRIES = 5

#: Cache key on the request object, so a request resolves its membership once.
_REQUEST_MEMBERSHIP_ATTR = "_findajob_membership"

#: Where a client may name the workspace it is addressing.
ORG_QUERY_PARAM = "org"
ORG_HEADER = "HTTP_X_FINDAJOB_ORG"


def default_workspace_name(user) -> str:
    """Derive a first workspace name from whatever identity the user has."""
    email = (getattr(user, "email", "") or "").strip()
    local_part = email.split("@", 1)[0] if "@" in email else email
    candidate = (local_part or str(getattr(user, "pk", "") or "")).strip()
    if not candidate:
        candidate = "workspace"
    return candidate[:MAX_ORG_NAME_LENGTH]


@transaction.atomic
def bootstrap_workspace(user, *, name: str | None = None) -> Membership:
    """Give ``user`` their own workspace.

    Creates **exactly** one :class:`~apps.tenancy.models.Organization` and one
    :class:`~apps.tenancy.models.Membership` with ``role=owner``. Nothing else:
    no invitation lookup, no subscription, no usage quota, no audit row (the
    caller owns its own audit trail and knows the request context).

    Idempotent. If ``user`` already owns a workspace the existing membership is
    returned untouched, so calling it from a re-entrant verify path can never
    mint a second organization.

    Intended call site: inside the same ``transaction.atomic`` block in
    ``apps.accounts`` that creates the user on first successful OTP verify.
    """
    existing = (
        Membership.objects.for_user(user, roles=[Role.OWNER])
        .select_related("org")
        .order_by("created_at", "id")
        .first()
    )
    if existing is not None:
        return existing

    workspace_name = (name or default_workspace_name(user)).strip()[:MAX_ORG_NAME_LENGTH]
    if not workspace_name:
        workspace_name = "workspace"

    org = None
    for attempt in range(_SLUG_RETRIES):
        slug = unique_org_slug(workspace_name)
        try:
            with transaction.atomic():
                org = Organization.objects.create(name=workspace_name, slug=slug)
            break
        except IntegrityError:
            if attempt == _SLUG_RETRIES - 1:
                raise
            logger.info("Organization slug collision on %r, retrying", slug)

    if org is None:  # pragma: no cover - the loop breaks with an org or re-raises
        raise IntegrityError("Could not allocate a unique organization slug.")

    return Membership.objects.create(org=org, user=user, role=Role.OWNER)


def memberships_for(user):
    """Every membership ``user`` holds, oldest first, with the org preloaded."""
    return (
        Membership.objects.for_user(user)
        .select_related("org")
        .order_by("created_at", "id")
    )


def resolve_membership(user, org_ref=None, *, memberships=None) -> Membership | None:
    """Pick the membership this request is acting under.

    ``org_ref`` is whatever the client asked for — an id as a string, an int, or
    ``None``. When it names a workspace the user is **not** a member of, this
    raises :class:`~apps.common.errors.NotOrgMember` rather than silently
    falling back: quietly serving a different tenant's workspace than the one
    that was requested is how cross-tenant bugs hide.

    With no ``org_ref`` the oldest membership wins, which for every MVP user is
    the workspace ``bootstrap_workspace`` created for them.
    """
    rows = list(memberships) if memberships is not None else list(memberships_for(user))
    if not rows:
        return None

    if org_ref in (None, ""):
        return rows[0]

    try:
        wanted = int(org_ref)
    except (TypeError, ValueError):
        raise NotOrgMember(detail="Unknown workspace.") from None

    for membership in rows:
        if membership.org_id == wanted:
            return membership
    # Same response whether the org does not exist or merely is not theirs —
    # never confirm the existence of another tenant's workspace.
    raise NotOrgMember(detail="Unknown workspace.")


def current_membership(request):
    """Resolve — and cache on the request — the membership in play.

    Returns ``None`` for anonymous users and for authenticated users who have
    no workspace yet. Raises :class:`~apps.common.errors.NotOrgMember` when the
    request explicitly names a workspace the user does not belong to.
    """
    cached = getattr(request, _REQUEST_MEMBERSHIP_ATTR, Ellipsis)
    if cached is not Ellipsis:
        return cached

    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        setattr(request, _REQUEST_MEMBERSHIP_ATTR, None)
        return None

    org_ref = None
    query_params = getattr(request, "query_params", None) or getattr(request, "GET", {})
    if query_params:
        org_ref = query_params.get(ORG_QUERY_PARAM)
    if not org_ref:
        org_ref = (getattr(request, "META", {}) or {}).get(ORG_HEADER)

    membership = resolve_membership(user, org_ref)
    setattr(request, _REQUEST_MEMBERSHIP_ATTR, membership)
    return membership


def current_org(request):
    """The :class:`~apps.tenancy.models.Organization` this request is scoped to."""
    membership = current_membership(request)
    return membership.org if membership is not None else None


def record_security_event(
    *,
    action: str,
    user=None,
    org=None,
    ip: str | None = None,
    user_agent: str = "",
    target=None,
    target_type: str = "",
    target_id: str = "",
    payload: dict | None = None,
) -> None:
    """Write one immutable :class:`~apps.tenancy.models.AuditLog` row.

    The one door ``apps.accounts`` is allowed to call through for audit
    logging — it may not import ``AuditLog``, ``Organization`` or
    ``Membership`` itself
    (``tests.accounts.test_models.ModuleBoundaryTests``), so every
    accounts-originated security event (a login, a logout, a wrong code)
    reaches the audit trail by calling this function by name, exactly the way
    it already calls :func:`bootstrap_workspace`.

    Never raises. An audit-log outage is a real problem, but it must never be
    able to turn into a login or logout outage, so any failure here is logged
    and swallowed rather than propagated.
    """
    try:
        AuditLog.objects.record(
            action=action,
            org=org,
            actor=user,
            target=target,
            target_type=target_type,
            target_id=target_id,
            payload=payload,
            ip=ip,
            user_agent=user_agent,
        )
    except Exception:  # noqa: BLE001 - any DB/config failure must not break auth
        logger.exception("Failed to write audit log for action=%s", action)
