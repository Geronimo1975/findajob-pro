"""The admin index as a Voltagent dashboard: KPI context and rendering."""

from __future__ import annotations

from django.urls import reverse

from tests.admin.support import AdminTestCase


class KpiContextTests(AdminTestCase):
    def _kpis(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        return {k["key"]: k for k in response.context["fj_kpis"]}

    def test_kpi_keys_and_order(self):
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(
            [k["key"] for k in response.context["fj_kpis"]],
            ["users", "organizations", "job_listings", "otps_today",
             "mailboxes", "suppressed"],
        )

    def test_counts_match_fixtures(self):
        kpis = self._kpis()
        # From AdminTestCase.setUpTestData: 3 users, 2 orgs, 1 job listing,
        # 4 OTP rows all created "today" by auto_now_add, 1 mailbox,
        # 1 suppressed address.
        self.assertEqual(kpis["users"]["value"], 3)
        self.assertEqual(kpis["organizations"]["value"], 2)
        self.assertEqual(kpis["job_listings"]["value"], 1)
        self.assertEqual(kpis["otps_today"]["value"], 4)
        self.assertEqual(kpis["mailboxes"]["value"], 1)
        self.assertEqual(kpis["suppressed"]["value"], 1)

    def test_every_kpi_has_a_label(self):
        for kpi in self._kpis().values():
            self.assertTrue(str(kpi["label"]))


class AnonymousRedirectTests(AdminTestCase):
    def test_anonymous_is_redirected_to_admin_login(self):
        self.client.logout()
        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("admin:login"), response["Location"])


class KpiEmptyDatabaseTests(AdminTestCase):
    """KPIs survive missing data (all-zero) without raising."""

    def test_zero_counts_render(self):
        from apps.accounts.models import EmailOTP, SuppressedAddress
        from apps.jobs.models import Application, JobListing
        from apps.outbox.models import MailboxConnection

        MailboxConnection.all_objects.all().delete()
        Application.all_objects.all().delete()
        JobListing.objects.all().delete()
        EmailOTP.objects.all().delete()
        SuppressedAddress.objects.all().delete()

        response = self.client.get(reverse("admin:index"))
        self.assertEqual(response.status_code, 200)
        kpis = {k["key"]: k["value"] for k in response.context["fj_kpis"]}
        self.assertEqual(kpis["job_listings"], 0)
        self.assertEqual(kpis["otps_today"], 0)
        self.assertEqual(kpis["mailboxes"], 0)
        self.assertEqual(kpis["suppressed"], 0)
