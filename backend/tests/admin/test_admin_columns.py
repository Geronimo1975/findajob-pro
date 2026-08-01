"""Computed columns, filters, actions — the parts that carry logic.

A ``list_display`` entry that is a plain field name cannot really be wrong. The
ones that can are the computed columns: they run code against a row, and every
one of them here reaches across a relation, derives a state, or annotates a
query. Each is tested for the value it produces *and* for the query cost of
producing it, because the two failure modes are "wrong number" and "one query
per row" and neither shows up in a smoke test.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib import admin
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.accounts.models import MAX_OTP_ATTEMPTS, EmailOTP, SuppressedAddress, User
from apps.legal.models import LegalDocument
from apps.tenancy.models import AuditLog, Membership, Organization, Role
from tests.admin.support import (
    AdminTestCase,
    change_url,
    changelist_url,
    make_document,
)


class UserColumnTests(AdminTestCase):
    def test_workspaces_column_lists_org_and_role(self):
        model_admin = admin.site._registry[User]
        self.assertEqual(model_admin.workspaces(self.member), "Tenant One (owner)")
        self.assertEqual(model_admin.workspaces(self.superuser), "—")

    def test_workspaces_column_is_rendered_on_the_changelist(self):
        response = self.client.get(changelist_url(User))
        self.assertContains(response, "Tenant One (owner)")

    def test_workspaces_column_does_not_scale_with_the_number_of_users(self):
        """The reverse relation is prefetched, not walked once per row."""
        with CaptureQueriesContext(connection) as baseline:
            self.client.get(changelist_url(User))

        for index in range(4):
            extra = User.objects.create_user(email=f"bulk{index}@example.de")
            Membership.objects.create(org=self.org, user=extra, role=Role.MEMBER)

        with CaptureQueriesContext(connection) as after:
            self.client.get(changelist_url(User))

        self.assertEqual(len(after.captured_queries), len(baseline.captured_queries))

    def test_workspaces_column_survives_an_unprefetched_instance(self):
        """The change form does not always come from the annotated changelist."""
        model_admin = admin.site._registry[User]
        fresh = User.objects.get(pk=self.member.pk)
        self.assertEqual(model_admin.workspaces(fresh), "Tenant One (owner)")


class UserActionTests(AdminTestCase):
    def _run(self, action: str, pks):
        return self.client.post(
            changelist_url(User),
            {
                "action": action,
                "_selected_action": [str(pk) for pk in pks],
                "index": "0",
            },
            follow=True,
        )

    def test_deactivate_and_reactivate_round_trip(self):
        self._run("deactivate_users", [self.member.pk])
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

        self._run("activate_users", [self.member.pk])
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)

    def test_deactivating_stamps_updated_at(self):
        """``.update()`` does not fire ``auto_now``; the action must do it."""
        before = self.member.updated_at
        self._run("deactivate_users", [self.member.pk])
        self.member.refresh_from_db()
        self.assertGreater(self.member.updated_at, before)

    def test_a_superuser_is_never_deactivated_in_bulk(self):
        other_root = User.objects.create_superuser(
            email="second-root@findajob.pro", password="another-admin-password-x"
        )
        response = self._run("deactivate_users", [other_root.pk, self.member.pk])

        other_root.refresh_from_db()
        self.member.refresh_from_db()
        self.assertTrue(other_root.is_active)
        self.assertFalse(self.member.is_active)
        self.assertContains(response, "skipped")

    def test_you_cannot_deactivate_yourself(self):
        self._run("deactivate_users", [self.superuser.pk])
        self.superuser.refresh_from_db()
        self.assertTrue(self.superuser.is_active)


class EmailOtpStateTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.model_admin = admin.site._registry[EmailOTP]

    def test_state_column_classifies_every_row(self):
        cases = {
            "live": self.live_otp,
            "used": self.used_otp,
            "expired": self.expired_otp,
            "burnt": self.burnt_otp,
        }
        for expected, row in cases.items():
            with self.subTest(state=expected):
                self.assertEqual(str(self.model_admin.state(row)), expected)

    def test_a_consumed_and_expired_row_reads_as_used(self):
        """Order of the checks is load-bearing, and mirrors the filter."""
        row = EmailOTP.objects.create(
            email="both@example.de",
            code_hash="0" * 64,
            expires_at=timezone.now() - timedelta(minutes=5),
            consumed_at=timezone.now(),
        )
        self.assertEqual(str(self.model_admin.state(row)), "used")

    def test_state_filter_matches_the_state_column(self):
        expected = {
            "live": {self.live_otp.pk},
            "used": {self.used_otp.pk},
            "expired": {self.expired_otp.pk},
            "burnt": {self.burnt_otp.pk},
        }
        for state, pks in expected.items():
            with self.subTest(state=state):
                response = self.client.get(changelist_url(EmailOTP), {"state": state})
                self.assertEqual(response.status_code, 200)
                selected = {row.pk for row in response.context["cl"].queryset}
                self.assertEqual(selected, pks)

    def test_the_four_states_partition_the_table(self):
        seen: set[int] = set()
        for state in ("live", "used", "expired", "burnt"):
            response = self.client.get(changelist_url(EmailOTP), {"state": state})
            pks = {row.pk for row in response.context["cl"].queryset}
            self.assertEqual(seen & pks, set(), f"{state} overlaps an earlier bucket")
            seen |= pks
        self.assertEqual(seen, set(EmailOTP.objects.values_list("pk", flat=True)))


class EmailOtpPurgeTests(AdminTestCase):
    def _purge(self, pks):
        return self.client.post(
            changelist_url(EmailOTP),
            {
                "action": "purge_expired",
                "_selected_action": [str(pk) for pk in pks],
                "index": "0",
            },
            follow=True,
        )

    def test_purging_everything_keeps_the_live_row(self):
        all_pks = list(EmailOTP.objects.values_list("pk", flat=True))
        response = self._purge(all_pks)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            set(EmailOTP.objects.values_list("pk", flat=True)), {self.live_otp.pk}
        )

    def test_purging_only_a_live_row_deletes_nothing(self):
        before = EmailOTP.objects.count()
        response = self._purge([self.live_otp.pk])

        self.assertEqual(EmailOTP.objects.count(), before)
        self.assertContains(response, "live code kept")

    def test_a_row_burnt_by_wrong_guesses_is_purgeable_even_before_it_expires(self):
        self.assertTrue(self.burnt_otp.expires_at > timezone.now())
        self.assertEqual(self.burnt_otp.attempts, MAX_OTP_ATTEMPTS)

        self._purge([self.burnt_otp.pk])
        self.assertFalse(EmailOTP.objects.filter(pk=self.burnt_otp.pk).exists())


class SuppressedAddressColumnTests(AdminTestCase):
    def test_suppressed_for_reports_an_age(self):
        model_admin = admin.site._registry[SuppressedAddress]
        value = str(model_admin.suppressed_for(self.suppressed))
        self.assertTrue(value)
        self.assertNotEqual(value, "—")

    def test_reason_filter_narrows_the_list(self):
        SuppressedAddress.suppress("complained@example.de", "complaint")
        response = self.client.get(
            changelist_url(SuppressedAddress), {"reason": "complaint"}
        )
        self.assertEqual(response.context["cl"].result_count, 1)


class LegalDocumentColumnTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.model_admin = admin.site._registry[LegalDocument]

    def test_sections_are_rendered_as_prose_not_json(self):
        html = str(self.model_admin.sections_preview(self.document))
        self.assertIn("1 Geltungsbereich", html)
        self.assertIn("Erster Abschnitt.", html)
        self.assertIn("<li", html)
        self.assertNotIn('"heading"', html)

    def test_section_markup_is_escaped_not_executed(self):
        hostile = make_document(
            doc_id="privacy",
            sections=[{"heading": "<script>x</script>", "body": "<b>bold</b>"}],
        )
        html = str(self.model_admin.sections_preview(hostile))
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;b&gt;bold&lt;/b&gt;", html)

    def test_malformed_sections_do_not_break_the_admin(self):
        """The page you would open to *diagnose* a bad row must not 500 on it.

        ``sections`` is a schema-free ``JSONField``, and re-hashing a value the
        canonicaliser rejects raises ``TypeError``. Both the changelist and the
        change form render ``hash_ok``, so one bad row would otherwise take out
        the whole page.
        """
        broken = LegalDocument.objects.create(
            doc_id="cookies",
            lang="en",
            version="broken",
            title="Broken",
            sections=["a bare string", {"body": "no heading"}, 42],
            content_hash="deadbeef",
            effective_from=timezone.now(),
            is_current=False,
        )
        self.assertEqual(self.client.get(change_url(broken)).status_code, 200)
        self.assertEqual(
            self.client.get(changelist_url(LegalDocument)).status_code, 200
        )
        self.assertIsNone(self.model_admin.hash_ok(broken))
        self.assertEqual(self.model_admin.section_count(broken), 3)

    def test_hash_ok_flips_when_the_text_drifts(self):
        self.assertTrue(self.model_admin.hash_ok(self.document))
        self.document.title = "Edited outside seed_legal"
        self.assertFalse(self.model_admin.hash_ok(self.document))

    def test_full_content_hash_is_visible_because_it_is_evidence(self):
        """The opposite of EmailOTP: this hash is over public text."""
        response = self.client.get(change_url(self.document))
        self.assertContains(response, self.document.content_hash)


class OrganizationColumnTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.model_admin = admin.site._registry[Organization]

    def test_member_count_and_owners(self):
        response = self.client.get(changelist_url(Organization))
        rows = {org.name: org for org in response.context["cl"].queryset}

        self.assertEqual(self.model_admin.member_count(rows["Tenant One"]), 1)
        self.assertEqual(self.model_admin.owners(rows["Tenant One"]), self.member.email)
        # Tenant Two's only membership is a plain member, so it has no owner.
        self.assertEqual(self.model_admin.member_count(rows["Tenant Two"]), 1)
        self.assertEqual(self.model_admin.owners(rows["Tenant Two"]), "—")

    def test_member_count_falls_back_when_the_row_is_not_annotated(self):
        fresh = Organization.objects.get(pk=self.org.pk)
        self.assertEqual(self.model_admin.member_count(fresh), 1)
        self.assertEqual(self.model_admin.owners(fresh), self.member.email)

    def test_member_count_is_not_multiplied_by_a_joined_search(self):
        """``Count(distinct=True)`` against ``search_fields``' reverse join."""
        Membership.objects.create(
            org=self.org,
            user=User.objects.create_user(email="second@example.de"),
            role=Role.MEMBER,
        )
        response = self.client.get(changelist_url(Organization), {"q": "example.de"})
        rows = {org.name: org for org in response.context["cl"].queryset}
        self.assertEqual(self.model_admin.member_count(rows["Tenant One"]), 2)

    def test_columns_do_not_scale_with_the_number_of_organizations(self):
        with CaptureQueriesContext(connection) as baseline:
            self.client.get(changelist_url(Organization))

        for index in range(4):
            org = Organization.objects.create(name=f"Bulk {index}", slug=f"bulk-{index}")
            Membership.objects.create(
                org=org,
                user=User.objects.create_user(email=f"owner{index}@example.de"),
                role=Role.OWNER,
            )

        with CaptureQueriesContext(connection) as after:
            self.client.get(changelist_url(Organization))

        self.assertEqual(len(after.captured_queries), len(baseline.captured_queries))

    def test_searching_by_a_members_email_finds_the_workspace(self):
        response = self.client.get(
            changelist_url(Organization), {"q": self.member.email}
        )
        names = [org.name for org in response.context["cl"].queryset]
        self.assertEqual(names, ["Tenant One"])


class MembershipColumnTests(AdminTestCase):
    def test_org_plan_column(self):
        model_admin = admin.site._registry[Membership]
        self.assertEqual(model_admin.org_plan(self.membership), "Free")

    def test_changelist_joins_org_and_user_once(self):
        with CaptureQueriesContext(connection) as baseline:
            self.client.get(changelist_url(Membership))

        for index in range(4):
            Membership.objects.create(
                org=self.org,
                user=User.objects.create_user(email=f"m{index}@example.de"),
                role=Role.MEMBER,
            )

        with CaptureQueriesContext(connection) as after:
            self.client.get(changelist_url(Membership))

        self.assertEqual(len(after.captured_queries), len(baseline.captured_queries))


class AuditLogColumnTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.model_admin = admin.site._registry[AuditLog]

    def test_target_column(self):
        self.assertEqual(
            self.model_admin.target(self.audit),
            f"tenancy.organization #{self.org.pk}",
        )
        empty = AuditLog.objects.record(action="auth.request_code")
        self.assertEqual(self.model_admin.target(empty), "—")

    def test_payload_is_pretty_printed_and_escaped(self):
        html = str(self.model_admin.payload_pretty(self.audit))
        self.assertIn("<pre", html)
        self.assertIn("[redacted]", html)
        self.assertIn("\n", html)

        hostile = AuditLog.objects.record(
            action="test.markup", payload={"note": "<script>x</script>"}
        )
        rendered = str(self.model_admin.payload_pretty(hostile))
        self.assertNotIn("<script>", rendered)

    def test_empty_payload_renders_a_dash(self):
        empty = AuditLog.objects.record(action="test.empty")
        self.assertEqual(self.model_admin.payload_pretty(empty), "—")

    def test_action_filter_is_bounded_and_usable(self):
        AuditLog.objects.record(action="org.created", org=self.org)
        response = self.client.get(changelist_url(AuditLog), {"action": "org.created"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 1)

    def test_filter_choices_are_capped(self):
        from apps.common.admin_support import distinct_values_filter

        for index in range(6):
            AuditLog.objects.record(action=f"bulk.action.{index}")

        capped = distinct_values_filter("action", "action", limit=3)
        request = self.client.get("/admin/").wsgi_request
        instance = capped(request, {}, AuditLog, self.model_admin)
        self.assertEqual(len(instance.lookup_choices), 3)

    def test_changelist_does_not_scale_with_the_number_of_events(self):
        with CaptureQueriesContext(connection) as baseline:
            self.client.get(changelist_url(AuditLog))

        for index in range(4):
            AuditLog.objects.record(
                action=f"bulk.{index}", org=self.org, actor=self.member
            )

        with CaptureQueriesContext(connection) as after:
            self.client.get(changelist_url(AuditLog))

        self.assertEqual(len(after.captured_queries), len(baseline.captured_queries))


class AdminNoteTests(AdminTestCase):
    """Every closed write path explains itself where the button is missing."""

    def test_notes_render_on_the_changelist(self):
        for model in (
            User,
            EmailOTP,
            SuppressedAddress,
            LegalDocument,
            Organization,
            Membership,
            AuditLog,
        ):
            model_admin = admin.site._registry[model]
            with self.subTest(model=model._meta.label):
                self.assertTrue(
                    model_admin.admin_note, f"{model._meta.label} has no admin_note"
                )
                response = self.client.get(changelist_url(model))
                self.assertContains(response, "findajob-admin-note")

    def test_the_note_is_escaped(self):
        from apps.legal.models import ConsentRecord

        model_admin = admin.site._registry[ConsentRecord]
        response = self.client.get(changelist_url(ConsentRecord))
        self.assertContains(response, "Append-only evidence")
        self.assertNotIn("<b>", str(model_admin.admin_note))
