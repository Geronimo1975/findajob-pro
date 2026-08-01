"""Org-scoped querysets — the multi-tenant safety net.

Every row in this product belongs to exactly one ``Organization``. The class of
bug that leaks one tenant's data into another's response is almost always a
*forgotten filter*, not a wrong one, so the default here is to make the
forgotten filter impossible rather than merely discouraged:

    >>> Membership.objects.all()                 # doctest: +SKIP
    Traceback (most recent call last):
    OrgScopeError: Membership queries must be scoped …

    >>> Membership.objects.for_org(org)          # doctest: +SKIP
    <OrgScopedQuerySet [...]>

Three ways to satisfy the guard:

``for_org(org)``    the workspace being addressed by this request
``for_user(user)``  every workspace that user is a member of
``unscoped()``      the deliberate, greppable escape hatch (admin, migrations,
                    management commands, and *only* those)

Reads are guarded; writes are not. ``.create()`` / ``.bulk_create()`` require
the caller to supply ``org`` explicitly anyway, so there is nothing to forget.

Consequence worth knowing
-------------------------
Django builds reverse related managers from ``Model._default_manager.__class__``,
so ``org.memberships.all()`` and ``user.memberships.all()`` inherit the guard and
raise. Use ``Membership.objects.for_org(org)`` / ``.for_user(user)`` instead.
Cascade deletes and forward FK traversal are unaffected: those go through
``Model._base_manager``, which stays a plain ``Manager``.
"""

from __future__ import annotations

from django.apps import apps as django_apps
from django.db import models


class OrgScopeError(RuntimeError):
    """Raised when an org-scoped queryset is evaluated without a scope."""


class OrgScopedQuerySet(models.QuerySet):
    """A queryset that refuses to be read until it has been scoped."""

    #: Name of the FK to ``Organization`` on this model.
    org_field = "org"

    #: Name of the FK to the user, when the model has one (``Membership``).
    #: Used so ``for_user()`` means "this user's rows" rather than
    #: "every row in every org this user belongs to".
    user_field: str | None = None

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._org_scoped = False

    # -- clone plumbing --------------------------------------------------
    # `_chain()` calls `_clone()`, and every queryset method that returns a new
    # queryset goes through one of the two, so overriding `_clone` is enough to
    # make the flag survive `.filter()`, `.exclude()`, `.values()`, `.none()`…

    def _clone(self):
        clone = super()._clone()
        clone._org_scoped = self._org_scoped
        clone.org_field = self.org_field
        clone.user_field = self.user_field
        return clone

    def _scoped(self, queryset: "OrgScopedQuerySet") -> "OrgScopedQuerySet":
        queryset._org_scoped = True
        return queryset

    # -- scoping helpers -------------------------------------------------

    def for_org(self, org) -> "OrgScopedQuerySet":
        """Restrict to a single organization. ``org`` may be a model or a pk."""
        org_id = getattr(org, "pk", org)
        if org_id is None:
            raise OrgScopeError(
                f"{self.model.__name__}.for_org() needs a saved Organization or a pk, "
                "got None."
            )
        return self._scoped(self.filter(**{f"{self.org_field}_id": org_id}))

    def for_orgs(self, orgs) -> "OrgScopedQuerySet":
        """Restrict to several organizations at once."""
        org_ids = [getattr(org, "pk", org) for org in orgs]
        if any(org_id is None for org_id in org_ids):
            raise OrgScopeError(
                f"{self.model.__name__}.for_orgs() received an unsaved Organization."
            )
        return self._scoped(self.filter(**{f"{self.org_field}_id__in": org_ids}))

    def for_user(self, user, *, roles=None) -> "OrgScopedQuerySet":
        """Restrict to what ``user`` may see.

        For ``Membership`` that is the user's own membership rows; for every
        other org-scoped model it is every row in the orgs the user belongs to.
        Anonymous or ``None`` users see nothing.
        """
        if user is None or not getattr(user, "is_authenticated", False):
            return self._scoped(self.none())

        membership_model = django_apps.get_model("tenancy", "Membership")

        if self.model is membership_model:
            queryset = self.filter(user=user)
            if roles:
                queryset = queryset.filter(role__in=list(roles))
            return self._scoped(queryset)

        # `_base_manager` is a plain Manager (we never set Meta.base_manager_name),
        # so this subquery is not itself guarded and cannot recurse.
        memberships = membership_model._base_manager.filter(user=user)
        if roles:
            memberships = memberships.filter(role__in=list(roles))
        return self._scoped(
            self.filter(**{f"{self.org_field}_id__in": memberships.values("org_id")})
        )

    def unscoped(self) -> "OrgScopedQuerySet":
        """Explicitly opt out of the guard.

        Legitimate callers: Django admin, data migrations, management commands,
        and tests asserting global state. Anything serving an HTTP request that
        calls this is a bug.
        """
        return self._scoped(self._chain())

    # -- the guard -------------------------------------------------------

    def _assert_scoped(self) -> None:
        if not self._org_scoped:
            raise OrgScopeError(
                f"{self.model.__name__} queries must be org-scoped. Use "
                f".for_org(org), .for_user(user) or — only outside request "
                f"handling — .unscoped()."
            )

    def _fetch_all(self):
        # Covers iteration, len(), bool(), list(), .get(), .first(), .last(),
        # .latest(), .in_bulk(), and every `values*()` variant.
        self._assert_scoped()
        return super()._fetch_all()

    def iterator(self, *args, **kwargs):
        # `iterator()` builds its own iterable class and never touches
        # `_fetch_all`, so it needs its own guard.
        self._assert_scoped()
        return super().iterator(*args, **kwargs)

    def count(self):
        self._assert_scoped()
        return super().count()

    def exists(self):
        self._assert_scoped()
        return super().exists()

    def aggregate(self, *args, **kwargs):
        self._assert_scoped()
        return super().aggregate(*args, **kwargs)

    def update(self, **kwargs):
        self._assert_scoped()
        return super().update(**kwargs)

    def delete(self):
        self._assert_scoped()
        return super().delete()


class OrgScopedManager(models.Manager.from_queryset(OrgScopedQuerySet)):
    """Default manager for org-scoped models.

    ``get_queryset()`` returns an **unscoped** queryset on purpose: the guard
    fires when the queryset is *evaluated*, not when it is built, so
    ``Model.objects.create(...)`` and ``Model.objects.for_org(org)`` both work
    while ``Model.objects.all()`` raises.
    """

    #: Migrations use the historical model's plain manager instead — a data
    #: migration has no request and therefore no org to scope to.
    use_in_migrations = False

    def __init__(self, *, org_field: str = "org", user_field: str | None = None):
        # Keyword-only with defaults so Django's generated reverse-related
        # manager (which instantiates the class with no arguments) still works.
        super().__init__()
        self._org_field = org_field
        self._user_field = user_field

    def get_queryset(self) -> OrgScopedQuerySet:
        queryset = super().get_queryset()
        queryset.org_field = self._org_field
        queryset.user_field = self._user_field
        return queryset


class OrganizationQuerySet(models.QuerySet):
    """Organizations are the scope root, so they are not themselves guarded."""

    def for_user(self, user, *, roles=None) -> "OrganizationQuerySet":
        if user is None or not getattr(user, "is_authenticated", False):
            return self.none()
        membership_model = django_apps.get_model("tenancy", "Membership")
        memberships = membership_model._base_manager.filter(user=user)
        if roles:
            memberships = memberships.filter(role__in=list(roles))
        return self.filter(pk__in=memberships.values("org_id"))


OrganizationManager = models.Manager.from_queryset(OrganizationQuerySet)
