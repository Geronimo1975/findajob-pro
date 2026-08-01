"""Org scoping rejects cross-tenant access.

Two independent layers are tested here:

1. **The manager guard** — an org-scoped queryset refuses to be *read* until it
   has been scoped, so a forgotten filter is an exception rather than a leak.
2. **The request layer** — ``/api/v1/me/`` and ``IsOrgMember`` never hand a user
   a workspace they are not a member of, whatever they put in ``?org=``.
"""

from __future__ import annotations

from django.contrib.auth.models import AnonymousUser
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.test import RequestFactory, TestCase

from apps.common.errors import NotOrgMember
from apps.tenancy.managers import OrgScopeError
from apps.tenancy.models import AuditLog, Membership, Organization, Role
from apps.tenancy.permissions import IsOrgAdmin, IsOrgMember, IsOrgOwner
from apps.tenancy.services import (
    bootstrap_workspace,
    current_membership,
    memberships_for,
    resolve_membership,
)
from tests.support import make_user

ME_URL = "/api/v1/me/"


class TenantFixture(TestCase):
    """Two fully independent tenants."""

    def setUp(self):
        self.alice = make_user("alice@tenant-a.de")
        self.bob = make_user("bob@tenant-b.de")
        self.alice_membership = bootstrap_workspace(self.alice, name="Tenant A")
        self.bob_membership = bootstrap_workspace(self.bob, name="Tenant B")
        self.org_a = self.alice_membership.org
        self.org_b = self.bob_membership.org


class ManagerGuardTests(TenantFixture):
    def test_unscoped_manager_reads_raise(self):
        cases = {
            "all": lambda: list(Membership.objects.all()),
            "count": lambda: Membership.objects.count(),
            "exists": lambda: Membership.objects.exists(),
            "first": lambda: Membership.objects.first(),
            "last": lambda: Membership.objects.last(),
            "filter": lambda: list(Membership.objects.filter(role=Role.OWNER)),
            "exclude": lambda: list(Membership.objects.exclude(pk=0)),
            "values": lambda: list(Membership.objects.values("id")),
            "values_list": lambda: list(Membership.objects.values_list("id", flat=True)),
            "get": lambda: Membership.objects.get(pk=self.alice_membership.pk),
            "aggregate": lambda: Membership.objects.aggregate(n=Count("id")),
            "in_bulk": lambda: Membership.objects.in_bulk(),
            "bool": lambda: bool(Membership.objects.all()),
            "len": lambda: len(Membership.objects.all()),
            "auditlog_all": lambda: list(AuditLog.objects.all()),
        }
        for name, call in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(OrgScopeError):
                    call()

    def test_unscoped_queryset_writes_raise(self):
        cases = {
            "update": lambda: Membership.objects.filter(role=Role.OWNER).update(
                role=Role.MEMBER
            ),
            "delete": lambda: Membership.objects.filter(role=Role.OWNER).delete(),
            "iterator": lambda: list(Membership.objects.filter(role=Role.OWNER).iterator()),
        }
        for name, call in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(OrgScopeError):
                    call()
        # Nothing was written despite the attempts.
        self.assertEqual(Membership.objects.unscoped().count(), 2)

    def test_creating_a_row_needs_no_scope(self):
        """Writes require an explicit ``org``, so there is nothing to forget."""
        carol = make_user("carol@tenant-a.de")
        membership = Membership.objects.create(
            org=self.org_a, user=carol, role=Role.MEMBER
        )
        self.assertEqual(membership.org_id, self.org_a.pk)

    def test_for_org_returns_only_that_org(self):
        self.assertEqual(
            [row.pk for row in Membership.objects.for_org(self.org_a)],
            [self.alice_membership.pk],
        )
        self.assertEqual(
            [row.pk for row in Membership.objects.for_org(self.org_b)],
            [self.bob_membership.pk],
        )

    def test_for_org_accepts_a_primary_key(self):
        self.assertEqual(
            [row.pk for row in Membership.objects.for_org(self.org_a.pk)],
            [self.alice_membership.pk],
        )

    def test_for_org_rejects_an_unsaved_organization(self):
        with self.assertRaises(OrgScopeError):
            Membership.objects.for_org(Organization(name="ghost", slug="ghost"))

    def test_for_user_returns_only_that_users_rows(self):
        self.assertEqual(
            [row.pk for row in Membership.objects.for_user(self.alice)],
            [self.alice_membership.pk],
        )
        self.assertEqual(
            [row.pk for row in Membership.objects.for_user(self.bob)],
            [self.bob_membership.pk],
        )

    def test_for_user_with_role_filter(self):
        self.assertEqual(
            Membership.objects.for_user(self.alice, roles=["owner"]).count(), 1
        )
        self.assertEqual(
            Membership.objects.for_user(self.alice, roles=["member"]).count(), 0
        )

    def test_anonymous_and_missing_users_see_nothing(self):
        self.assertEqual(Membership.objects.for_user(AnonymousUser()).count(), 0)
        self.assertEqual(Membership.objects.for_user(None).count(), 0)

    def test_scope_survives_chaining(self):
        chained = (
            Membership.objects.for_org(self.org_a)
            .select_related("org")
            .filter(role=Role.OWNER)
            .exclude(pk=0)
            .order_by("-id")
        )
        self.assertEqual([row.pk for row in chained], [self.alice_membership.pk])

    def test_unscoped_is_the_documented_escape_hatch(self):
        self.assertEqual(Membership.objects.unscoped().count(), 2)
        self.assertEqual(Membership.all_objects.count(), 2)

    def test_audit_log_scoping(self):
        AuditLog.objects.record(action="test.a", org=self.org_a, actor=self.alice)
        AuditLog.objects.record(action="test.b", org=self.org_b, actor=self.bob)

        self.assertEqual(
            [row.action for row in AuditLog.objects.for_org(self.org_a)], ["test.a"]
        )
        self.assertEqual(
            [row.action for row in AuditLog.objects.for_user(self.bob)], ["test.b"]
        )


class AuditLogTests(TenantFixture):
    def test_payload_secrets_are_scrubbed_before_write(self):
        entry = AuditLog.objects.record(
            action="auth.verify",
            org=self.org_a,
            actor=self.alice,
            payload={
                "code": "123456",
                "email": "alice@tenant-a.de",
                "nested": {"api_key": "sk-ant-secret", "ok": 1},
            },
        )
        entry.refresh_from_db()

        self.assertEqual(entry.payload["code"], "[redacted]")
        self.assertEqual(entry.payload["nested"]["api_key"], "[redacted]")
        self.assertEqual(entry.payload["email"], "alice@tenant-a.de")
        self.assertEqual(entry.payload["nested"]["ok"], 1)
        self.assertNotIn("123456", str(entry.payload))

    def test_rows_are_append_only(self):
        entry = AuditLog.objects.record(action="test.x", org=self.org_a)
        entry.action = "test.y"
        with self.assertRaises(ValueError):
            entry.save()

    def test_target_is_derived_from_an_instance(self):
        entry = AuditLog.objects.record(
            action="org.created", org=self.org_a, target=self.org_a
        )
        self.assertEqual(entry.target_type, "tenancy.organization")
        self.assertEqual(entry.target_id, str(self.org_a.pk))

    def test_orgless_events_are_allowed(self):
        entry = AuditLog.objects.record(action="auth.request_code", ip="203.0.113.7")
        self.assertIsNone(entry.org_id)
        self.assertEqual(entry.ip, "203.0.113.7")


class MeEndpointScopingTests(TenantFixture):
    def test_each_user_sees_only_their_own_workspace(self):
        self.client.force_login(self.alice)
        body = self.client.get(ME_URL).json()
        self.assertEqual(body["org"]["id"], self.org_a.pk)
        self.assertEqual(len(body["memberships"]), 1)

        self.client.force_login(self.bob)
        body = self.client.get(ME_URL).json()
        self.assertEqual(body["org"]["id"], self.org_b.pk)

    def test_asking_for_someone_elses_org_is_rejected(self):
        self.client.force_login(self.alice)

        response = self.client.get(ME_URL, {"org": self.org_b.pk})

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_org_member")

    def test_asking_for_a_nonexistent_org_looks_identical(self):
        """Never confirm whether another tenant's workspace exists."""
        self.client.force_login(self.alice)

        real_other = self.client.get(ME_URL, {"org": self.org_b.pk})
        made_up = self.client.get(ME_URL, {"org": 10_000_000})

        self.assertEqual(real_other.status_code, made_up.status_code)
        self.assertEqual(real_other.json(), made_up.json())

    def test_garbage_org_parameter_is_rejected_not_a_500(self):
        self.client.force_login(self.alice)
        response = self.client.get(ME_URL, {"org": "'; DROP TABLE --"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "not_org_member")

    def test_org_header_is_honoured_for_a_real_membership(self):
        second = Membership.objects.create(
            org=self.org_b, user=self.alice, role=Role.MEMBER
        )
        self.client.force_login(self.alice)

        body = self.client.get(
            ME_URL, headers={"x-findajob-org": str(self.org_b.pk)}
        ).json()

        self.assertEqual(body["org"]["id"], self.org_b.pk)
        self.assertEqual(body["membership_role"], "member")
        self.assertEqual(len(body["memberships"]), 2)
        self.assertIn(second.pk, [entry["id"] for entry in body["memberships"]])


class ResolveMembershipTests(TenantFixture):
    def test_defaults_to_the_oldest_membership(self):
        self.assertEqual(
            resolve_membership(self.alice, None).pk, self.alice_membership.pk
        )

    def test_returns_none_for_a_user_with_no_workspace(self):
        self.assertIsNone(resolve_membership(make_user(), None))

    def test_rejects_a_foreign_org(self):
        with self.assertRaises(NotOrgMember):
            resolve_membership(self.alice, self.org_b.pk)

    def test_rejects_a_non_numeric_org_reference(self):
        with self.assertRaises(NotOrgMember):
            resolve_membership(self.alice, "not-a-number")

    def test_memberships_for_is_ordered_and_preloads_org(self):
        Membership.objects.create(org=self.org_b, user=self.alice, role=Role.MEMBER)
        with self.assertNumQueries(1):
            rows = list(memberships_for(self.alice))
            self.assertEqual(
                [row.org.pk for row in rows], [self.org_a.pk, self.org_b.pk]
            )


class PermissionTests(TenantFixture):
    def setUp(self):
        super().setUp()
        self.factory = RequestFactory()

    def _request(self, user, **params):
        request = self.factory.get(ME_URL, params)
        request.user = user
        return request

    def test_anonymous_is_denied(self):
        self.assertFalse(
            IsOrgMember().has_permission(self._request(AnonymousUser()), None)
        )

    def test_member_of_a_workspace_is_allowed(self):
        self.assertTrue(IsOrgMember().has_permission(self._request(self.alice), None))

    def test_user_without_a_workspace_is_denied(self):
        self.assertFalse(IsOrgMember().has_permission(self._request(make_user()), None))

    def test_owner_passes_owner_and_admin_gates(self):
        request = self._request(self.alice)
        self.assertTrue(IsOrgOwner().has_permission(request, None))
        self.assertTrue(IsOrgAdmin().has_permission(request, None))

    def test_plain_member_fails_the_admin_gate(self):
        carol = make_user("carol@tenant-a.de")
        Membership.objects.create(org=self.org_a, user=carol, role=Role.MEMBER)
        request = self._request(carol)

        self.assertTrue(IsOrgMember().has_permission(request, None))
        self.assertFalse(IsOrgAdmin().has_permission(request, None))
        self.assertFalse(IsOrgOwner().has_permission(request, None))

    def test_object_permission_blocks_another_tenants_row(self):
        request = self._request(self.alice)
        permission = IsOrgMember()

        self.assertTrue(
            permission.has_object_permission(request, None, self.alice_membership)
        )
        self.assertFalse(
            permission.has_object_permission(request, None, self.bob_membership)
        )
        self.assertTrue(permission.has_object_permission(request, None, self.org_a))
        self.assertFalse(permission.has_object_permission(request, None, self.org_b))

    def test_current_membership_is_resolved_once_per_request(self):
        request = self._request(self.alice)
        first = current_membership(request)
        with self.assertNumQueries(0):
            second = current_membership(request)
        self.assertIs(first, second)


class TenancyModelConstraintTests(TenantFixture):
    def test_org_slugs_are_unique(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Organization.objects.create(name="Clash", slug=self.org_a.slug)

    def test_one_membership_per_user_per_org(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Membership.objects.create(
                    org=self.org_a, user=self.alice, role=Role.MEMBER
                )

    def test_blank_org_name_is_rejected_by_the_database(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Organization.objects.create(name="", slug="blank-name")

    def test_members_count(self):
        self.assertEqual(self.org_a.members_count, 1)
        Membership.objects.create(
            org=self.org_a, user=make_user("dave@tenant-a.de"), role=Role.MEMBER
        )
        self.assertEqual(self.org_a.members_count, 2)
