"""``apps.tenancy.services.record_security_event`` — the accounts→tenancy audit bridge.

``apps.accounts`` is forbidden from importing ``AuditLog``, ``Organization`` or
``Membership`` directly (``tests.accounts.test_models.ModuleBoundaryTests``), so
this function is the one door it may call through, by name, exactly the way it
already calls ``bootstrap_workspace``.
"""

from __future__ import annotations

from unittest import mock

from django.test import TestCase

from apps.tenancy import services
from apps.tenancy.models import AuditLog
from tests.support import make_user
from tests.tenancy.test_bootstrap_workspace import snapshot_row_counts


class RecordSecurityEventTests(TestCase):
    def test_writes_an_audit_row_with_the_given_fields(self):
        user = make_user("george@example.de")
        membership = services.bootstrap_workspace(user)

        services.record_security_event(
            action="auth.login_succeeded",
            user=user,
            org=membership.org,
            ip="203.0.113.5",
            user_agent="pytest",
            target_type="accounts.emailotp",
            target_id="42",
            payload={"reason": "ok"},
        )

        row = AuditLog.all_objects.get()
        self.assertEqual(row.action, "auth.login_succeeded")
        self.assertEqual(row.actor_id, user.pk)
        self.assertEqual(row.org_id, membership.org_id)
        self.assertEqual(row.ip, "203.0.113.5")
        self.assertEqual(row.user_agent, "pytest")
        self.assertEqual(row.target_type, "accounts.emailotp")
        self.assertEqual(row.target_id, "42")
        self.assertEqual(row.payload, {"reason": "ok"})

    def test_org_and_user_are_optional(self):
        services.record_security_event(action="auth.login_failed")
        row = AuditLog.all_objects.get()
        self.assertIsNone(row.org_id)
        self.assertIsNone(row.actor_id)

    def test_a_write_failure_is_swallowed_not_raised(self):
        with mock.patch.object(
            AuditLog.objects, "record", side_effect=RuntimeError("db is down")
        ):
            services.record_security_event(action="auth.login_succeeded")  # must not raise
        self.assertEqual(AuditLog.all_objects.count(), 0)

    def test_it_writes_no_other_table(self):
        before = snapshot_row_counts()
        services.record_security_event(action="auth.login_failed")
        after = snapshot_row_counts()
        changed = {label for label, count in after.items() if before.get(label) != count}
        self.assertEqual(changed, {AuditLog._meta.label})
