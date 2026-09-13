"""Shared fixtures for the admin suite.

Builds exactly one row per registered model, so a data-driven smoke test can
open every changelist *and* every change form without special-casing anything.

Like ``tests.accounts.support``, this depends on *app* APIs rather than on
another test package's helpers: a helper in a sibling test package is not a
contract, and coupling to it makes an unrelated rename break these tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import (
    MAX_OTP_ATTEMPTS,
    EmailOTP,
    SuppressedAddress,
    SuppressionReason,
    User,
    hash_code,
)
from apps.legal.hashing import compute_content_hash
from apps.legal.models import ConsentRecord, LegalDocument
from apps.tenancy.models import AuditLog, Membership, Organization, Role
from apps.tenancy.services import bootstrap_workspace

#: The plaintext behind the fixture OTP. Never stored; only ``hash_code`` of it
#: reaches the database, which is the invariant the security tests assert.
FIXTURE_OTP_CODE = "424242"

SUPERUSER_EMAIL = "root@findajob.pro"
SUPERUSER_PASSWORD = "admin-suite-password-not-a-real-secret"

DOC_VERSION = "2026-08-01"


def make_document(
    *,
    doc_id: str = "terms",
    lang: str = "de",
    version: str = DOC_VERSION,
    title: str = "Allgemeine Geschäftsbedingungen",
    sections=None,
    note: str = "Die deutsche Fassung ist die rechtlich verbindliche.",
    is_current: bool = True,
) -> LegalDocument:
    """A correctly-hashed document row."""
    if sections is None:
        sections = [
            {"heading": "1 Geltungsbereich", "body": "Erster Abschnitt.\nZweite Zeile."},
            {"heading": "2 Vertragsschluss", "body": "Zweiter Abschnitt."},
        ]
    return LegalDocument.objects.create(
        doc_id=doc_id,
        lang=lang,
        version=version,
        title=title,
        sections=sections,
        note=note,
        content_hash=compute_content_hash(
            doc_id=doc_id,
            lang=lang,
            version=version,
            title=title,
            sections=sections,
        ),
        effective_from=datetime(2026, 8, 1, tzinfo=dt_timezone.utc),
        is_current=is_current,
    )


def changelist_url(model) -> str:
    meta = model._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")


def change_url(obj) -> str:
    meta = obj._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[obj.pk])


def add_url(model) -> str:
    meta = model._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_add")


def delete_url(obj) -> str:
    meta = obj._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_delete", args=[obj.pk])


def first_row(model):
    """One instance of ``model``, read through an **unguarded** manager.

    ``Model._base_manager`` is ``all_objects`` for the org-scoped models
    (``Meta.base_manager_name``) and a plain auto-created ``Manager`` for
    everything else, so this never trips ``OrgScopeError``.
    """
    return model._base_manager.order_by("pk").first()


class AdminTestCase(TestCase):
    """A logged-in superuser plus one row of everything."""

    @classmethod
    def setUpTestData(cls) -> None:
        cls.superuser = User.objects.create_superuser(
            email=SUPERUSER_EMAIL, password=SUPERUSER_PASSWORD
        )
        cls.member = User.objects.create_user(email="member@example.de", lang="de")
        cls.outsider = User.objects.create_user(email="outsider@example.de")

        cls.membership = bootstrap_workspace(cls.member, name="Tenant One")
        cls.org = cls.membership.org
        cls.second_org = Organization.objects.create(
            name="Tenant Two", slug="tenant-two"
        )
        cls.second_membership = Membership.objects.create(
            org=cls.second_org, user=cls.outsider, role=Role.MEMBER
        )

        cls.audit = AuditLog.objects.record(
            action="auth.verify_code",
            org=cls.org,
            actor=cls.member,
            target=cls.org,
            payload={
                "code": FIXTURE_OTP_CODE,
                "email": cls.member.email,
                "nested": {"api_key": "sk-secret", "ok": 1},
            },
            ip="203.0.113.7",
            user_agent="Mozilla/5.0 (admin tests)",
        )

        cls.document = make_document()
        cls.consent = ConsentRecord.objects.create(
            user=cls.member,
            type="terms",
            granted=True,
            doc_version=cls.document.version,
            content_hash=cls.document.content_hash,
            doc_lang=cls.document.lang,
            ip="203.0.113.7",
            user_agent="Mozilla/5.0 (admin tests)",
            ui_language="en",
        )

        now = timezone.now()
        cls.live_otp = EmailOTP.objects.create(
            email=cls.member.email,
            code_hash=hash_code(FIXTURE_OTP_CODE, email=cls.member.email),
            expires_at=now + timedelta(minutes=10),
            request_ip="203.0.113.7",
        )
        cls.used_otp = EmailOTP.objects.create(
            email="used@example.de",
            code_hash=hash_code("111111", email="used@example.de"),
            expires_at=now + timedelta(minutes=10),
            consumed_at=now,
        )
        cls.expired_otp = EmailOTP.objects.create(
            email="expired@example.de",
            code_hash=hash_code("222222", email="expired@example.de"),
            expires_at=now - timedelta(minutes=1),
        )
        cls.burnt_otp = EmailOTP.objects.create(
            email="burnt@example.de",
            code_hash=hash_code("333333", email="burnt@example.de"),
            expires_at=now + timedelta(minutes=10),
            attempts=MAX_OTP_ATTEMPTS,
        )

        cls.suppressed = SuppressedAddress.suppress(
            "bounced@example.de", SuppressionReason.HARD_BOUNCE
        )
        cls.group = Group.objects.create(name="Support")

        from apps.tenancy.outbox import crypto as outbox_crypto
        from apps.tenancy.outbox.models import MailboxConnection

        cls.mailbox = MailboxConnection.objects.create(
            org=cls.org,
            user=cls.member,
            provider="smtp",
            email_address=cls.member.email,
            display_name="Member One",
            credentials_encrypted=outbox_crypto.encrypt_credentials(
                {"host": "mail.example.de", "port": 587,
                 "username": cls.member.email, "password": "fixture-secret",
                 "use_tls": True}
            ),
        )

        from django.core.files.base import ContentFile
        from django.core.management import call_command

        from apps.jobs.models import Application, Document as JobDocument, JobListing

        call_command("seed_platforms", verbosity=0)
        cls.job_listing = JobListing.objects.create(
            platform_id="remoteok",
            title="Senior Django Engineer",
            company="Loftware",
            url="https://remoteok.com/jobs/101",
            tags=["Django", "DRF"],
            platform_specific_data={"apply_email": "jobs@loftware.io"},
            scraped_at=timezone.now(),
        )
        cls.job_document = JobDocument.objects.create(
            org=cls.org,
            user=cls.member,
            name="CV — Backend (EN).pdf",
            kind="cv",
            file=ContentFile(b"%PDF-1.4", name="cv-backend-en.pdf"),
        )
        cls.application = Application.objects.create(
            org=cls.org, user=cls.member, job_listing=cls.job_listing
        )
        cls.application.documents.add(
            cls.job_document, through_defaults={"position": 0}
        )

    def setUp(self) -> None:
        super().setUp()
        self.client.force_login(self.superuser)
