"""``bootstrap_workspace()`` creates one org + one membership and nothing else.

Implementation spec §2 makes the *absence* of behaviour the contract: no
Invitation lookup, no Subscription, no UsageQuota. Auto-accepting an invitation
at login is a cross-tenant account-capture hole, so the strongest form of this
test asserts the source itself never mentions those names.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

from django.apps import apps as django_apps
from django.test import TestCase

from apps.tenancy import services
from apps.tenancy.models import AuditLog, Membership, Organization, Role
from tests.support import make_user


def snapshot_row_counts() -> dict[str, int]:
    """Row count for every concrete model in the project.

    Uses ``_base_manager`` so the org-scope guard on ``Membership`` and
    ``AuditLog`` does not interfere — this is exactly the "tests asserting
    global state" escape hatch the guard documents.
    """
    counts: dict[str, int] = {}
    for model in django_apps.get_models():
        if model._meta.abstract or model._meta.proxy or not model._meta.managed:
            continue
        counts[model._meta.label] = model._base_manager.count()
    return counts


class BootstrapWorkspaceTests(TestCase):
    def test_creates_exactly_one_organization_and_one_membership(self):
        user = make_user("andrei@popescu.dev")

        membership = services.bootstrap_workspace(user)

        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(Membership.all_objects.count(), 1)
        self.assertEqual(membership.user_id, user.pk)
        self.assertEqual(membership.role, Role.OWNER)
        self.assertEqual(membership.org_id, Organization.objects.get().pk)

    def test_touches_no_other_table(self):
        """Nothing anywhere in the schema changes except the two tables."""
        user = make_user()
        before = snapshot_row_counts()

        services.bootstrap_workspace(user)

        after = snapshot_row_counts()
        changed = {
            label: (before[label], count)
            for label, count in after.items()
            if before.get(label) != count
        }
        self.assertEqual(
            set(changed),
            {Organization._meta.label, Membership._meta.label},
            f"bootstrap_workspace wrote to unexpected tables: {changed}",
        )
        self.assertEqual(AuditLog.all_objects.count(), 0)

    def test_is_idempotent(self):
        user = make_user()

        first = services.bootstrap_workspace(user)
        second = services.bootstrap_workspace(user)

        self.assertEqual(first.pk, second.pk)
        self.assertEqual(Organization.objects.count(), 1)
        self.assertEqual(Membership.all_objects.count(), 1)

    def test_two_users_get_two_separate_workspaces(self):
        first = services.bootstrap_workspace(make_user("a@example.de"))
        second = services.bootstrap_workspace(make_user("b@example.de"))

        self.assertNotEqual(first.org_id, second.org_id)
        self.assertEqual(Organization.objects.count(), 2)

    def test_slug_collision_is_resolved(self):
        """Two users whose email local part is identical still both get a slug."""
        first = services.bootstrap_workspace(make_user("chris@one.de"))
        second = services.bootstrap_workspace(make_user("chris@two.de"))

        self.assertEqual(first.org.name, "chris")
        self.assertEqual(second.org.name, "chris")
        self.assertNotEqual(first.org.slug, second.org.slug)
        self.assertEqual(Organization.objects.filter(slug=first.org.slug).count(), 1)

    def test_explicit_name_is_honoured(self):
        membership = services.bootstrap_workspace(
            make_user(), name="Popescu Career Services"
        )
        self.assertEqual(membership.org.name, "Popescu Career Services")
        self.assertEqual(membership.org.slug, "popescu-career-services")

    def test_default_workspace_name_never_returns_empty(self):
        self.assertEqual(
            services.default_workspace_name(SimpleNamespace(email="ana@findajob.pro", pk=3)),
            "ana",
        )
        self.assertEqual(
            services.default_workspace_name(SimpleNamespace(email="", pk=7)), "7"
        )
        self.assertEqual(
            services.default_workspace_name(SimpleNamespace(email="", pk=None)),
            "workspace",
        )


class ModuleBoundaryTests(TestCase):
    """Spec §2 — the names that must never appear in the bootstrap path."""

    FORBIDDEN = ("Invitation", "Subscription", "UsageQuota")

    def test_services_module_never_references_unbuilt_models(self):
        source = inspect.getsource(services)
        # Strip the module docstring: it quotes the spec, which names Invitation.
        body = source.split('"""', 2)[-1]
        for name in self.FORBIDDEN:
            with self.subTest(name=name):
                self.assertIsNone(
                    re.search(rf"\b{name}\b", body),
                    f"apps.tenancy.services must not reference {name}.",
                )

    def test_no_model_named_invitation_subscription_or_usagequota_exists(self):
        labels = {model.__name__ for model in django_apps.get_models()}
        self.assertEqual(labels & set(self.FORBIDDEN), set())

    def test_bootstrap_workspace_signature_takes_only_a_user(self):
        signature = inspect.signature(services.bootstrap_workspace)
        parameters = list(signature.parameters)
        self.assertEqual(parameters[0], "user")
        self.assertEqual(
            [p for p in parameters if p != "user"],
            ["name"],
            "bootstrap_workspace must not grow arguments that imply other models.",
        )
