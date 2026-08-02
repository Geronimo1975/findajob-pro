"""Jobs models — scoping, constraints, and the platform catalog seed."""

from __future__ import annotations

from django.core.files.base import ContentFile
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.jobs.models import (
    Application,
    ApplicationDocument,
    ApplicationStatus,
    Document,
    JobListing,
    Platform,
)
from apps.tenancy.managers import OrgScopeError
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user


def seed():
    call_command("seed_platforms", verbosity=0)


def make_listing(platform_slug="remoteok", **extra) -> JobListing:
    platform = Platform.objects.get(slug=platform_slug)
    defaults = {
        "title": "Senior Django Engineer",
        "company": "Loftware",
        "url": "https://remoteok.com/jobs/101",
        "tags": ["Django", "DRF"],
        "platform_specific_data": {"apply_email": "jobs@loftware.io"},
        "scraped_at": timezone.now(),
    }
    defaults.update(extra)
    return JobListing.objects.create(platform=platform, **defaults)


class PlatformCatalogTests(TestCase):
    def test_seed_creates_the_five_launch_platforms_idempotently(self):
        seed()
        self.assertEqual(Platform.objects.count(), 5)
        seed()
        self.assertEqual(Platform.objects.count(), 5)

    def test_auto_send_is_exactly_the_low_risk_email_platforms(self):
        seed()
        self.assertEqual(
            set(Platform.objects.filter(auto_send=True).values_list(
                "slug", flat=True
            )),
            {"wwr", "remoteok"},
        )

    def test_auto_send_on_a_risky_platform_is_rejected_by_the_database(self):
        seed()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Platform.objects.filter(slug="linkedin").update(auto_send=True)


class JobListingTests(TestCase):
    def setUp(self):
        seed()

    def test_listings_are_global_and_unscoped(self):
        make_listing()
        self.assertEqual(JobListing.objects.count(), 1)  # no OrgScopeError

    def test_rescrape_of_the_same_url_is_rejected(self):
        make_listing()
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_listing()

    def test_urlless_listings_do_not_collide(self):
        make_listing(url="")
        make_listing(url="", title="Another")
        self.assertEqual(JobListing.objects.count(), 2)

    def test_apply_email_property(self):
        listing = make_listing()
        self.assertEqual(listing.apply_email, "jobs@loftware.io")
        bare = make_listing(url="", platform_specific_data={})
        self.assertIsNone(bare.apply_email)


class ApplicationScopingTests(TestCase):
    def setUp(self):
        seed()
        self.user = make_user()
        self.org = bootstrap_workspace(self.user).org
        self.listing = make_listing()

    def _application(self, **extra) -> Application:
        defaults = {"org": self.org, "user": self.user, "job_listing": self.listing}
        defaults.update(extra)
        return Application.objects.create(**defaults)

    def test_unscoped_reads_raise(self):
        self._application()
        with self.assertRaises(OrgScopeError):
            list(Application.objects.all())
        with self.assertRaises(OrgScopeError):
            list(Document.objects.all())

    def test_scoped_reads_work(self):
        application = self._application()
        self.assertEqual(
            list(Application.objects.for_org(self.org)), [application]
        )

    def test_default_status_is_pending(self):
        self.assertEqual(self._application().status, ApplicationStatus.PENDING)

    def test_one_application_per_member_per_listing(self):
        self._application()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self._application()

    def test_other_tenant_does_not_see_the_application(self):
        self._application()
        stranger = make_user()
        stranger_org = bootstrap_workspace(stranger).org
        self.assertEqual(
            Application.objects.for_org(stranger_org).count(), 0
        )

    def test_deleting_a_listing_with_applications_is_protected(self):
        self._application()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.listing.delete()

    def test_attachments_round_trip_in_order(self):
        application = self._application()
        docs = [
            Document.objects.create(
                org=self.org, user=self.user, name=f"doc-{i}.pdf", kind="cv",
                file=ContentFile(b"%PDF-1.4", name=f"doc-{i}.pdf"),
            )
            for i in range(2)
        ]
        application.documents.add(docs[1], through_defaults={"position": 0})
        application.documents.add(docs[0], through_defaults={"position": 1})
        # The reverse manager inherits the org-scope guard (by design, like
        # org.memberships.all()); attachments are read through the through
        # table, whose ordering is the attachment order.
        with self.assertRaises(OrgScopeError):
            list(application.documents.all())
        attached = [
            ad.document
            for ad in ApplicationDocument.objects.filter(
                application=application
            ).select_related("document")
        ]
        self.assertEqual(attached, [docs[1], docs[0]])
