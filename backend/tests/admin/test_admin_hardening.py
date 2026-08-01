"""The admin controls the security review found missing.

Each test here corresponds to a finding that was real when it was written — a
measured hole, not a hypothetical. They are grouped by the property they defend
so a future edit that removes the guard fails with a name that says what broke.
"""

from __future__ import annotations

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse

from apps.accounts.admin import EmailOTPAdmin, SuppressedAddressAdmin, UserAdmin
from apps.accounts.models import User
from apps.common import admin_throttle
from apps.tenancy.admin import OrganizationAdmin
from apps.tenancy.models import Organization


def _perm(codename: str) -> Permission:
    return Permission.objects.get(codename=codename)


class CredentialLookupTests(TestCase):
    """A column kept out of ``search_fields`` is still reachable by query string.

    ``ModelAdmin.lookup_allowed`` is permissive by default, so
    ``?password__startswith=pbkdf2`` turns the changelist into a one-bit oracle
    that recovers a hash character by character without ever rendering it.
    """

    def test_user_admin_refuses_password_lookups(self):
        admin = UserAdmin(User, None)
        for lookup in ("password", "password__startswith", "password__contains"):
            with self.subTest(lookup=lookup):
                self.assertFalse(admin.lookup_allowed(lookup, "x", None))

    def test_user_admin_still_allows_ordinary_lookups(self):
        admin = UserAdmin(User, None)
        self.assertTrue(admin.lookup_allowed("is_verified__exact", "1", None))

    def test_emailotp_admin_refuses_code_hash_lookups(self):
        from apps.accounts.models import EmailOTP

        admin = EmailOTPAdmin(EmailOTP, None)
        for lookup in ("code_hash", "code_hash__startswith"):
            with self.subTest(lookup=lookup):
                self.assertFalse(admin.lookup_allowed(lookup, "a", None))


class PrivilegeEscalationTests(TestCase):
    """Staff must not be able to promote themselves."""

    def setUp(self):
        self.factory = RequestFactory()
        self.staff = User.objects.create_user(
            email="staff@findajob.pro", password="x", is_staff=True
        )
        self.root = User.objects.create_superuser(
            email="root@findajob.pro", password="x"
        )
        self.admin = UserAdmin(User, None)

    def _request(self, user):
        request = self.factory.get("/admin/accounts/user/")
        request.user = user
        return request

    def test_staff_cannot_edit_the_privilege_fields(self):
        readonly = self.admin.get_readonly_fields(self._request(self.staff))
        for field in ("is_superuser", "is_staff", "groups", "user_permissions"):
            self.assertIn(field, readonly)

    def test_superuser_can_still_edit_them(self):
        readonly = self.admin.get_readonly_fields(self._request(self.root))
        for field in ("is_superuser", "is_staff", "groups", "user_permissions"):
            self.assertNotIn(field, readonly)

    def test_staff_cannot_change_or_delete_a_superuser(self):
        request = self._request(self.staff)
        self.assertFalse(self.admin.has_change_permission(request, self.root))
        self.assertFalse(self.admin.has_delete_permission(request, self.root))


class MemberColumnLeakTests(TestCase):
    """The org page must not leak its member list through computed columns.

    The Membership inline restores Django's permission check; ``member_count``
    and ``owners`` read the same table through the same unguarded manager and
    are rendered on the same pages, so they need the same gate.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.admin = OrganizationAdmin(Organization, None)
        self.blind = User.objects.create_user(
            email="blind@findajob.pro", password="x", is_staff=True
        )
        self.blind.user_permissions.add(_perm("view_organization"))
        self.seeing = User.objects.create_user(
            email="seeing@findajob.pro", password="x", is_staff=True
        )
        self.seeing.user_permissions.add(
            _perm("view_organization"), _perm("view_membership")
        )

    def _request(self, user):
        request = self.factory.get("/admin/tenancy/organization/")
        # Permission caches are per-instance; refetch so the grants above apply.
        request.user = User.objects.get(pk=user.pk)
        return request

    def test_member_columns_are_hidden_without_membership_permission(self):
        columns = self.admin.get_list_display(self._request(self.blind))
        self.assertNotIn("member_count", columns)
        self.assertNotIn("owners", columns)

    def test_member_columns_are_shown_with_it(self):
        columns = self.admin.get_list_display(self._request(self.seeing))
        self.assertIn("member_count", columns)
        self.assertIn("owners", columns)

    def test_the_inline_disappears_too(self):
        self.assertEqual(self.admin.get_inlines(self._request(self.blind)), ())
        self.assertTrue(self.admin.get_inlines(self._request(self.seeing)))


class SuppressedAddressTests(TestCase):
    def test_email_is_read_only(self):
        from apps.accounts.models import SuppressedAddress

        admin = SuppressedAddressAdmin(SuppressedAddress, None)
        self.assertIn("email", admin.readonly_fields)


class AdminLoginThrottleTests(TestCase):
    """Failed admin sign-ins are budgeted per (username, subnet)."""

    def setUp(self):
        self.factory = RequestFactory()

    def _request(self, ip="203.0.113.7"):
        request = self.factory.post("/admin/login/", {"username": "root@findajob.pro"})
        request.META["REMOTE_ADDR"] = ip
        return request

    def test_the_budget_freezes_after_the_configured_failures(self):
        request = self._request()
        for _ in range(admin_throttle.ADMIN_LOGIN_MAX_FAILURES + 1):
            admin_throttle.record_admin_login_failure("root@findajob.pro", "203.0.113.7")
        with self.assertRaises(PermissionDenied):
            admin_throttle.enforce_admin_login_budget(request, "root@findajob.pro")

    def test_a_different_subnet_is_a_different_budget(self):
        for _ in range(admin_throttle.ADMIN_LOGIN_MAX_FAILURES + 1):
            admin_throttle.record_admin_login_failure("root@findajob.pro", "203.0.113.7")
        # Same username, different network: the mailbox owner must not be locked
        # out by somebody else's guessing run.
        admin_throttle.enforce_admin_login_budget(
            self._request(ip="198.51.100.9"), "root@findajob.pro"
        )

    def test_a_get_is_never_throttled(self):
        response = self.client.get(reverse("admin:login"))
        self.assertEqual(response.status_code, 200)
