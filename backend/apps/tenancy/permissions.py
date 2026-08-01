"""DRF permissions for org-scoped views.

``IsAuthenticated`` is the project-wide default (see
``REST_FRAMEWORK['DEFAULT_PERMISSION_CLASSES']``); these add the tenancy
dimension on top of it.

Object-level checks only run when a view calls
``self.check_object_permissions(request, obj)`` — which ``GenericAPIView``'s
``get_object()`` does for you. They are a second line of defence, not the first:
the first is always querying through ``Model.objects.for_org(org)``.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

from apps.tenancy.models import ADMIN_ROLES, Organization, Role
from apps.tenancy.services import current_membership


def _org_id_of(obj) -> int | None:
    """Best-effort extraction of the organization a model instance belongs to."""
    if isinstance(obj, Organization):
        return obj.pk
    org_id = getattr(obj, "org_id", None)
    if org_id is not None:
        return org_id
    org = getattr(obj, "org", None)
    return getattr(org, "pk", None)


class IsOrgMember(BasePermission):
    """Authenticated **and** a member of the workspace this request addresses.

    Subclass and set :attr:`allowed_roles` to narrow it further; see
    :class:`IsOrgAdmin` and :class:`IsOrgOwner`.
    """

    message = "You are not a member of this workspace."

    #: ``None`` means "any role".
    allowed_roles: frozenset[str] | None = None

    def has_permission(self, request, view) -> bool:
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return False
        # Raises NotOrgMember (403, envelope code `not_org_member`) when the
        # request names a workspace the user does not belong to.
        membership = current_membership(request)
        if membership is None:
            return False
        if self.allowed_roles is None:
            return True
        return membership.role in self.allowed_roles

    def has_object_permission(self, request, view, obj) -> bool:
        membership = current_membership(request)
        if membership is None:
            return False
        if self.allowed_roles is not None and membership.role not in self.allowed_roles:
            return False
        org_id = _org_id_of(obj)
        if org_id is None:
            # A model with no organization is not tenant data; refuse rather
            # than guess. Views over non-tenant models should not use this class.
            return False
        return org_id == membership.org_id


class IsOrgAdmin(IsOrgMember):
    """Owner or admin of the current workspace."""

    message = "This action requires workspace admin rights."
    allowed_roles = frozenset(ADMIN_ROLES)


class IsOrgOwner(IsOrgMember):
    """Owner of the current workspace."""

    message = "This action requires workspace owner rights."
    allowed_roles = frozenset({Role.OWNER.value})
