"""``apps.tenancy.services.record_security_event`` — the accounts→tenancy audit bridge.

``apps.accounts`` is forbidden from importing ``AuditLog``, ``Organization`` or
``Membership`` directly (``tests.accounts.test_models.ModuleBoundaryTests``), so
this function is the one door it may call through, by name, exactly the way it
already calls ``bootstrap_workspace``.
"""

from __future__ import annotations

from unittest import mock

from django.db import IntegrityError, transaction
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

    def test_a_real_db_failure_does_not_poison_the_callers_outer_transaction(self):
        """Regression: a DB-level failure inside the audit write must not mark
        the caller's outer ``transaction.atomic()`` for rollback.

        A plain ``RuntimeError`` (as in the test above) never touches the
        database, so it can't reproduce what actually happens in
        ``_complete_login``: a real ``DatabaseError`` propagating out of a
        cursor operation sets ``connection.needs_rollback`` on the *whole*
        transaction, not just the innermost block, unless that write runs in
        its own nested ``transaction.atomic()`` (a savepoint). This test
        forces a genuine ``IntegrityError`` from SQLite (AuditLog.action is
        NOT NULL) and asserts that work committed earlier in the same outer
        atomic block survives.
        """

        def write_a_row_with_no_action(**_kwargs):
            AuditLog.all_objects.create(action=None)

        with mock.patch.object(
            AuditLog.objects, "record", side_effect=write_a_row_with_no_action
        ):
            with transaction.atomic():
                user = make_user("george@example.de")
                services.record_security_event(
                    action="auth.login_succeeded", user=user
                )  # must not raise, and must not poison the block above

        # The user created earlier in the same outer atomic block must have
        # survived — proving the outer transaction was not silently rolled
        # back by the audit write's IntegrityError.
        self.assertTrue(type(user).objects.filter(pk=user.pk).exists())
        self.assertEqual(AuditLog.all_objects.count(), 0)

    def test_without_the_savepoint_a_swallowed_db_failure_silently_loses_committed_work(self):
        """Sanity check that the bug this suite guards against is real.

        Mirrors the pre-fix shape of ``record_security_event``: the audit
        write's ``IntegrityError`` is caught with a bare try/except — no
        nested ``transaction.atomic()`` savepoint — inside the caller's
        outer atomic block. Django still marks the connection for rollback
        (``connection.needs_rollback``), so the outer block's ``__exit__``
        rolls back *silently* (no exception reaches the caller) and the user
        created just before the audit write vanishes — exactly the "200 with
        a session cookie for data that no longer exists" bug.
        """
        with transaction.atomic():
            user = make_user("george2@example.de")
            try:
                AuditLog.all_objects.create(action=None)
            except IntegrityError:
                pass  # exactly what record_security_event's try/except does

        self.assertFalse(type(user).objects.filter(pk=user.pk).exists())

    def test_it_writes_no_other_table(self):
        before = snapshot_row_counts()
        services.record_security_event(action="auth.login_failed")
        after = snapshot_row_counts()
        changed = {label for label, count in after.items() if before.get(label) != count}
        self.assertEqual(changed, {AuditLog._meta.label})
