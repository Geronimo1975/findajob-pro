"""What the admin must refuse, and what it must never print.

Two independent classes of assertion:

* **Write refusal.** ``LegalDocument``, ``ConsentRecord`` and ``AuditLog`` are
  evidence. Checking ``has_add_permission()`` is not enough — a mixin can be
  present while a subclass quietly re-enables the view, so every refusal is
  asserted over HTTP *and* by counting rows afterwards.
* **Secret rendering.** ``EmailOTP.code_hash`` and ``User.password`` are
  credential material. A ``readonly_fields`` entry added without thought prints
  them, and nothing else in the suite would notice.
"""

from __future__ import annotations

from django.contrib import admin

from apps.accounts.models import EmailOTP, SuppressedAddress, User
from apps.common.admin_support import short_digest
from apps.legal.models import ConsentRecord, LegalDocument
from apps.tenancy.models import AuditLog
from tests.admin.support import (
    FIXTURE_OTP_CODE,
    AdminTestCase,
    add_url,
    change_url,
    changelist_url,
    delete_url,
)

#: Every model whose admin must refuse add, change *and* delete.
READ_ONLY_MODELS = (LegalDocument, ConsentRecord, AuditLog)


class ReadOnlyModelTests(AdminTestCase):
    def test_permission_methods_all_say_no(self):
        request = self.client.get("/admin/").wsgi_request
        for model in READ_ONLY_MODELS:
            model_admin = admin.site._registry[model]
            with self.subTest(model=model._meta.label):
                self.assertFalse(model_admin.has_add_permission(request))
                self.assertFalse(model_admin.has_change_permission(request))
                self.assertFalse(model_admin.has_delete_permission(request))

    def test_add_view_is_refused(self):
        for model in READ_ONLY_MODELS:
            with self.subTest(model=model._meta.label):
                before = model._base_manager.count()
                self.assertEqual(self.client.get(add_url(model)).status_code, 403)
                self.assertEqual(self.client.post(add_url(model), {}).status_code, 403)
                self.assertEqual(model._base_manager.count(), before)

    def test_change_view_is_view_only(self):
        for model, obj in (
            (LegalDocument, self.document),
            (ConsentRecord, self.consent),
            (AuditLog, self.audit),
        ):
            with self.subTest(model=model._meta.label):
                # Readable...
                self.assertEqual(self.client.get(change_url(obj)).status_code, 200)
                # ...but not writable.
                self.assertEqual(self.client.post(change_url(obj), {}).status_code, 403)

    def test_change_view_post_changes_nothing(self):
        original_title = self.document.title
        self.client.post(change_url(self.document), {"title": "Rewritten"})
        self.document.refresh_from_db()
        self.assertEqual(self.document.title, original_title)

    def test_delete_view_is_refused(self):
        for model, obj in (
            (LegalDocument, self.document),
            (ConsentRecord, self.consent),
            (AuditLog, self.audit),
        ):
            with self.subTest(model=model._meta.label):
                before = model._base_manager.count()
                self.assertEqual(self.client.get(delete_url(obj)).status_code, 403)
                self.assertEqual(
                    self.client.post(delete_url(obj), {"post": "yes"}).status_code, 403
                )
                self.assertEqual(model._base_manager.count(), before)

    def test_no_bulk_delete_action_is_offered(self):
        """Django drops ``delete_selected`` once delete permission is off."""
        request = self.client.get("/admin/").wsgi_request
        for model in READ_ONLY_MODELS:
            model_admin = admin.site._registry[model]
            with self.subTest(model=model._meta.label):
                self.assertNotIn("delete_selected", model_admin.get_actions(request))

    def test_the_generated_form_has_no_editable_field_at_all(self):
        """The end state that actually matters, however it was reached.

        Some raw columns are replaced by a rendered surrogate rather than listed
        in ``readonly_fields`` — ``LegalDocument.sections`` by
        ``sections_preview``, ``AuditLog.payload`` by ``payload_pretty``. Both
        routes must end in the same place: a form with nothing to submit.
        """
        request = self.client.get("/admin/").wsgi_request
        for model, obj in (
            (LegalDocument, self.document),
            (ConsentRecord, self.consent),
            (AuditLog, self.audit),
        ):
            model_admin = admin.site._registry[model]
            form_class = model_admin.get_form(request, obj, change=True)
            with self.subTest(model=model._meta.label):
                self.assertEqual(
                    dict(form_class.base_fields),
                    {},
                    f"{model._meta.label} exposes an editable field",
                )

    def test_the_mixin_defaults_to_every_field_when_none_are_declared(self):
        """A column added to a read-only model is read-only from day one."""
        from apps.common.admin_support import ReadOnlyAdminMixin

        class BareAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
            pass

        model_admin = BareAdmin(LegalDocument, admin.site)
        request = self.client.get("/admin/").wsgi_request
        self.assertEqual(
            set(model_admin.get_readonly_fields(request)),
            {field.name for field in LegalDocument._meta.fields},
        )


class WriteRestrictedModelTests(AdminTestCase):
    """Models that are *partly* closed, each for its own reason."""

    def test_email_otp_cannot_be_added_or_changed_but_can_be_deleted(self):
        model_admin = admin.site._registry[EmailOTP]
        request = self.client.get("/admin/").wsgi_request

        self.assertFalse(model_admin.has_add_permission(request))
        self.assertFalse(model_admin.has_change_permission(request))
        self.assertTrue(model_admin.has_delete_permission(request))

        self.assertEqual(self.client.post(add_url(EmailOTP), {}).status_code, 403)
        self.assertEqual(
            self.client.post(change_url(self.live_otp), {}).status_code, 403
        )
        self.assertEqual(self.client.get(delete_url(self.live_otp)).status_code, 200)

    def test_suppressed_address_cannot_be_added_but_can_be_released(self):
        model_admin = admin.site._registry[SuppressedAddress]
        request = self.client.get("/admin/").wsgi_request

        self.assertFalse(model_admin.has_add_permission(request))
        # Re-classification and deletion both stay available: deleting a row is
        # the supported un-suppress.
        self.assertTrue(model_admin.has_change_permission(request))
        self.assertTrue(model_admin.has_delete_permission(request))
        self.assertEqual(self.client.get(add_url(SuppressedAddress)).status_code, 403)


class InlinePermissionTests(AdminTestCase):
    """The Membership inline reads through the unguarded manager — and only that.

    Replacing ``InlineModelAdmin.get_queryset`` wholesale (which the unguarded
    mixin does) drops Django's rule that an inline shows nothing to a staff user
    holding neither view nor change permission on the inline model. Without the
    restatement in ``MembershipInline.get_queryset``, an account with only
    ``view_organization`` would read every workspace's member list.
    """

    def _limited_staff(self):
        from django.contrib.auth.models import Permission

        staff = User.objects.create_user(email="limited@findajob.pro", is_staff=True)
        staff.user_permissions.add(
            Permission.objects.get(
                codename="view_organization", content_type__app_label="tenancy"
            )
        )
        # Re-fetch: permission lookups are cached on the instance.
        return User.objects.get(pk=staff.pk)

    def _inline(self):
        from apps.tenancy.models import Organization

        org_admin = admin.site._registry[Organization]
        return org_admin.inlines[0](Organization, admin.site)

    def test_staff_without_membership_permission_sees_no_inline_rows(self):
        from django.test import RequestFactory

        inline = self._inline()
        request = RequestFactory().get("/admin/")
        request.user = self._limited_staff()

        self.assertFalse(inline.has_view_or_change_permission(request))
        self.assertEqual(inline.get_queryset(request).count(), 0)

    def test_a_permitted_user_still_sees_the_rows(self):
        from django.test import RequestFactory

        inline = self._inline()
        request = RequestFactory().get("/admin/")
        request.user = self.superuser

        self.assertGreaterEqual(inline.get_queryset(request).count(), 1)


class SecretRenderingTests(AdminTestCase):
    def _pages_for(self, model, obj):
        return {
            "changelist": self.client.get(changelist_url(model)).content.decode(),
            "change form": self.client.get(change_url(obj)).content.decode(),
        }

    def test_otp_code_hash_is_never_rendered(self):
        code_hash = self.live_otp.code_hash
        self.assertEqual(len(code_hash), 64)

        for page, html in self._pages_for(EmailOTP, self.live_otp).items():
            with self.subTest(page=page):
                self.assertNotIn(code_hash, html)
                # Not even a prefix: a prefix is directly comparable against the
                # stored column, which is the whole reason a *derived* digest is
                # used for the fingerprint instead.
                self.assertNotIn(code_hash[:16], html)
                self.assertNotIn(FIXTURE_OTP_CODE, html)

    def test_otp_fingerprint_is_shown_and_is_not_a_prefix_of_the_hash(self):
        fingerprint = short_digest(self.live_otp.code_hash)
        self.assertEqual(len(fingerprint), 8)
        self.assertFalse(self.live_otp.code_hash.startswith(fingerprint))

        for page, html in self._pages_for(EmailOTP, self.live_otp).items():
            with self.subTest(page=page):
                self.assertIn(fingerprint, html)

    def test_code_hash_is_not_searchable(self):
        """A searchable hash column is an oracle: it confirms a guess."""
        model_admin = admin.site._registry[EmailOTP]
        self.assertNotIn("code_hash", model_admin.search_fields)
        self.assertNotIn("code_hash", model_admin.list_display)

        response = self.client.get(
            changelist_url(EmailOTP), {"q": self.live_otp.code_hash}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 0)

    def test_password_hash_is_never_rendered(self):
        self.superuser.refresh_from_db()
        password_hash = self.superuser.password
        self.assertTrue(password_hash)

        for page, html in self._pages_for(User, self.superuser).items():
            with self.subTest(page=page):
                self.assertNotIn(password_hash, html)
                self.assertNotIn(password_hash[:20], html)

    def test_password_field_is_not_on_the_user_form(self):
        model_admin = admin.site._registry[User]
        request = self.client.get("/admin/").wsgi_request
        form_class = model_admin.get_form(request, self.superuser, change=True)
        self.assertNotIn("password", form_class.base_fields)

    def test_password_state_reports_usability_not_the_hash(self):
        model_admin = admin.site._registry[User]
        self.assertIn(
            "admin", str(model_admin.password_state(self.superuser)).lower()
        )
        self.assertIn(
            "email-code", str(model_admin.password_state(self.member)).lower()
        )

    def test_audit_payload_is_shown_redacted(self):
        html = self.client.get(change_url(self.audit)).content.decode()
        self.assertIn("[redacted]", html)
        self.assertNotIn(FIXTURE_OTP_CODE, html)
        self.assertNotIn("sk-secret", html)


class UserCreationTests(AdminTestCase):
    def test_a_user_added_from_admin_gets_an_unusable_password(self):
        """Not a blank one — blank is a state no login path expects."""
        response = self.client.post(
            add_url(User),
            {
                "email": "created-in-admin@example.de",
                "lang": "en",
                "is_active": "on",
                "groups": [],
                "user_permissions": [],
            },
            follow=True,
        )
        self.assertEqual(response.status_code, 200)

        created = User.objects.get(email="created-in-admin@example.de")
        self.assertFalse(created.has_usable_password())
        self.assertNotEqual(created.password, "")
