"""The two third-party admin packages, and the seams that make them behave.

Each test here covers something that fails **silently** rather than loudly —
which is why they exist at all. In order of how hard they are to notice:

* A ``change_list_template`` collision. Two mixins, one string attribute; the
  loser's feature just is not on the page, with no error and no log line.
* An export that reaches for ``Model.objects`` on an org-scoped model. That one
  *is* loud (``OrgScopeError``) but only on the row that nobody exports until
  the day an auditor asks.
* An object-action button rendered as a ``<a href>`` instead of a form. The
  button works, so nothing looks wrong; it is simply a CSRF-less GET that
  deletes a row.
* An export whose column list quietly grew a field somebody added to the model.
"""

from __future__ import annotations

import csv
import io
from unittest import mock

from django.contrib import admin
from django.test import SimpleTestCase
from django.urls import reverse

from apps.accounts.models import SuppressedAddress
from apps.common.admin_integrations import (
    EXPORT_FORMATS,
    EvidenceExportAdminMixin,
    ObjectActionsAdminMixin,
)
from apps.common.admin_support import CHANGE_LIST_NOTE_TEMPLATE
from apps.legal.admin import ConsentRecordResource
from apps.legal.models import ConsentRecord
from apps.tenancy.admin import AuditLogResource
from apps.tenancy.models import AuditLog
from tests.admin.support import AdminTestCase, change_url, changelist_url

#: The models wired for export, and the resource each one must use.
EXPORTABLE = ((ConsentRecord, ConsentRecordResource), (AuditLog, AuditLogResource))


def export_action_url(model) -> str:
    meta = model._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_export")


class ExportConfigurationTests(SimpleTestCase):
    def test_only_csv_and_json_are_offered(self):
        """Not the library's default list.

        TSV, YAML and HTML are all things tablib will happily produce and that
        nobody downstream reads. HTML in particular is an evidence table turned
        into markup a browser interprets.
        """
        names = {fmt().get_title() for fmt in EXPORT_FORMATS}
        self.assertEqual(names, {"csv", "json"})

    def test_the_setting_is_not_what_configures_it(self):
        """``IMPORT_EXPORT_FORMATS`` must stay unset.

        It has to hold format *classes*, so honouring it from settings would
        mean importing tablib while settings are still being read. The class
        attribute on the mixin shadows the library's property instead — this
        asserts nobody "fixed" that by adding the setting back.
        """
        from django.conf import settings

        self.assertFalse(hasattr(settings, "IMPORT_EXPORT_FORMATS"))

    def test_the_admins_expose_those_formats(self):
        for model, _resource in EXPORTABLE:
            with self.subTest(model=model._meta.label):
                self.assertEqual(
                    list(admin.site._registry[model].export_formats), EXPORT_FORMATS
                )

    def test_no_admin_can_import(self):
        """Import into an append-only ledger would be a way to forge evidence."""
        for model_admin in admin.site._registry.values():
            with self.subTest(admin=type(model_admin).__name__):
                self.assertFalse(hasattr(model_admin, "import_action"))

    def test_the_change_form_export_button_is_off(self):
        """Its handler bypasses ``get_queryset``.

        ``ExportActionMixin.response_change`` is hard-coded to
        ``self.model.objects.filter(pk=…)`` — the *guarded* manager on
        ``AuditLog``, which raises ``OrgScopeError``. The changelist action is
        the only supported path.
        """
        for model, _resource in EXPORTABLE:
            with self.subTest(model=model._meta.label):
                self.assertFalse(
                    admin.site._registry[model].show_change_form_export
                )


class ExportColumnContractTests(SimpleTestCase):
    """A field added to a model must not join the export by accident."""

    def test_consent_export_carries_the_whole_evidence_chain(self):
        columns = set(ConsentRecordResource.Meta.fields)
        for required in (
            "created_at",
            "user__email",
            "type",
            "granted",
            "doc_version",
            "doc_lang",
            "content_hash",
        ):
            with self.subTest(column=required):
                self.assertIn(required, columns)

    def test_audit_export_carries_the_scrubbed_payload(self):
        self.assertIn("payload", AuditLogResource.Meta.fields)

    def test_neither_export_names_a_field_that_no_longer_exists(self):
        """Catches a rename. The resource would otherwise export a blank column."""
        for model, resource in EXPORTABLE:
            declared = {field.name for field in model._meta.get_fields()}
            for column in resource.Meta.fields:
                root = column.split("__", 1)[0]
                with self.subTest(model=model._meta.label, column=column):
                    self.assertIn(root, declared)


class ExportBehaviourTests(AdminTestCase):
    """The HTTP flow, end to end.

    Driven through the real two-step UI — select rows on the changelist, then
    confirm format and columns — rather than by calling the resource directly,
    because every failure this file is guarding against happens in the admin
    plumbing between those two requests, not in the serialisation.
    """

    def export_payload(self, model, *, fmt: str = "csv", items=()) -> dict:
        """Build a valid POST body from the admin's own export form.

        Hand-writing it is not an option: the per-column checkboxes are named
        after the resource class (``consentrecordresource_content_hash``) and
        the format field is an *index* into the offered formats, so a literal
        payload would silently rot the moment either changes.
        """
        model_admin = admin.site._registry[model]
        formats = model_admin.get_export_formats()
        form = model_admin.get_export_form_class()(
            formats=formats,
            resources=model_admin.get_export_resource_classes(None),
        )
        payload = {
            name: "on"
            for name, field in form.fields.items()
            if field.__class__.__name__ == "BooleanField"
        }
        payload["resource"] = "0"
        payload["format"] = str(
            next(i for i, f in enumerate(formats) if f().get_title() == fmt)
        )
        if items:
            payload["export_items"] = [str(pk) for pk in items]
        return payload

    def test_the_export_action_is_offered_on_a_read_only_changelist(self):
        """``ReadOnlyAdminMixin`` turns off add/change/delete.

        The export action needs none of them, so it has to survive that — if it
        did not, the two models that most need exporting would be the two that
        cannot.
        """
        for model, _resource in EXPORTABLE:
            with self.subTest(model=model._meta.label):
                response = self.client.get(changelist_url(model))
                self.assertEqual(response.status_code, 200)
                offered = dict(admin.site._registry[model].get_actions(
                    response.wsgi_request
                ))
                self.assertIn("export_admin_action", offered)

    def test_selecting_rows_and_exporting_reaches_the_confirmation_form(self):
        """Step one of the real flow, on the org-scoped model.

        This is where ``OrgScopeError`` would surface if the action reached for
        ``AuditLog.objects`` instead of inheriting ``UnguardedQuerySetMixin``'s
        queryset.
        """
        response = self.client.post(
            changelist_url(AuditLog),
            {
                "action": "export_admin_action",
                "_selected_action": [str(self.audit.pk)],
                "index": "0",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("form", response.context)

    def test_exporting_the_audit_log_returns_csv_and_not_an_orgscopeerror(self):
        response = self.client.post(
            export_action_url(AuditLog),
            self.export_payload(AuditLog, items=[self.audit.pk]),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual([row["action"] for row in rows], ["auth.verify_code"])

    def test_a_consent_export_contains_the_row_and_its_hash(self):
        response = self.client.post(
            export_action_url(ConsentRecord), self.export_payload(ConsentRecord)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        rows = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user__email"], self.member.email)
        self.assertEqual(rows[0]["content_hash"], self.consent.content_hash)

    def test_json_is_the_other_offered_format(self):
        response = self.client.post(
            export_action_url(ConsentRecord),
            self.export_payload(ConsentRecord, fmt="json"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json")

    def test_an_export_is_recorded_to_the_audit_log(self):
        """"Who took a copy of the evidence" is part of the evidence.

        Not ``django.contrib.admin.models.LogEntry``: nothing in
        django-import-export writes one on export (``IMPORT_EXPORT_SKIP_ADMIN_LOG``
        governs *imports*), and ``LogEntry`` would have to mislabel an export as
        an ADDITION or a CHANGE anyway.
        """
        before = AuditLog.all_objects.count()
        response = self.client.post(
            export_action_url(ConsentRecord), self.export_payload(ConsentRecord)
        )
        self.assertEqual(response["Content-Type"], "text/csv")

        recorded = AuditLog.all_objects.filter(action="admin.export")
        self.assertEqual(AuditLog.all_objects.count(), before + 1)
        entry = recorded.latest("created_at")
        self.assertEqual(entry.actor, self.superuser)
        self.assertEqual(entry.target_type, "legal.consentrecord")
        self.assertEqual(entry.payload["format"], "csv")
        self.assertEqual(entry.payload["rows"], 1)

    def test_a_failed_export_records_nothing(self):
        """The choke point is the *file*, not the page.

        An invalid form re-renders the export page with a 200, and that must
        not look like somebody took a copy.
        """
        before = AuditLog.all_objects.count()
        response = self.client.post(export_action_url(ConsentRecord), {})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response["Content-Type"], "text/csv")
        self.assertEqual(AuditLog.all_objects.count(), before)

    def test_a_broken_audit_log_does_not_lose_the_export(self):
        """The file has already been produced when the recording runs.

        Losing the audit row is bad; losing the export the operator asked for
        because writing the audit row failed is worse and much harder to
        diagnose from the admin.
        """
        with mock.patch.object(
            AuditLog.objects, "record", side_effect=RuntimeError("audit down")
        ), self.assertLogs("apps.common.admin_integrations", level="ERROR"):
            response = self.client.post(
                export_action_url(ConsentRecord), self.export_payload(ConsentRecord)
            )
        self.assertEqual(response["Content-Type"], "text/csv")


class ObjectActionsCompositionTests(SimpleTestCase):
    """The changelist-template collision, pinned.

    ``DjangoObjectActions`` and ``AdminNoteMixin`` both want
    ``change_list_template``. Only one value survives and MRO order decides it
    silently — so whichever loses, its feature vanishes from the page with no
    error at all.
    """

    def test_the_note_template_wins_the_attribute(self):
        self.assertEqual(
            ObjectActionsAdminMixin.change_list_template, CHANGE_LIST_NOTE_TEMPLATE
        )

    def test_and_chains_onto_the_object_actions_template(self):
        self.assertEqual(
            ObjectActionsAdminMixin.admin_note_base_template,
            "django_object_actions/change_list.html",
        )

    def test_the_suppressed_address_admin_uses_the_composed_mixin(self):
        model_admin = admin.site._registry[SuppressedAddress]
        self.assertIsInstance(model_admin, ObjectActionsAdminMixin)

    def test_buttons_post_rather_than_link(self):
        """A GET-triggered delete has no CSRF token and is link-followable."""
        from django.conf import settings

        self.assertEqual(settings.DJANGO_OBJECT_ACTIONS_DEFAULT_HTTP_METHOD, "POST")

    def test_there_is_no_bulk_unsuppress(self):
        """Per-address decision. A bulk button would undo a morning of bounce
        handling in one click."""
        self.assertEqual(
            tuple(admin.site._registry[SuppressedAddress].changelist_actions), ()
        )


class ChangelistNoteStillRendersTests(AdminTestCase):
    """Both features on the page at once — the whole point of the composition."""

    def test_the_note_survives_on_the_object_actions_changelist(self):
        response = self.client.get(changelist_url(SuppressedAddress))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "findajob-admin-note")

    def test_every_other_changelist_note_still_renders(self):
        """The template's parent became a variable; a page with no
        ``admin_note_base_template`` in context must still extend the stock
        changelist rather than fail to resolve."""
        for model in (ConsentRecord, AuditLog):
            with self.subTest(model=model._meta.label):
                response = self.client.get(changelist_url(model))
                self.assertContains(response, "findajob-admin-note")


class UnsuppressActionTests(AdminTestCase):
    def action_url(self, obj) -> str:
        meta = obj._meta
        return reverse(
            f"admin:{meta.app_label}_{meta.model_name}_actions",
            args=[obj.pk, "unsuppress"],
        )

    def test_the_button_is_on_the_change_form(self):
        response = self.client.get(change_url(self.suppressed))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "objectaction-item")
        self.assertContains(response, "Un-suppress")

    def test_it_deletes_the_row_and_redirects_to_the_changelist(self):
        response = self.client.post(self.action_url(self.suppressed))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], changelist_url(SuppressedAddress))
        self.assertFalse(
            SuppressedAddress.objects.filter(pk=self.suppressed.pk).exists()
        )

    def test_the_address_can_be_mailed_again_afterwards(self):
        """The actual point of the button, asserted through the model API the
        OTP path uses rather than through the row."""
        self.assertTrue(SuppressedAddress.is_suppressed("bounced@example.de"))
        self.client.post(self.action_url(self.suppressed))
        self.assertFalse(SuppressedAddress.is_suppressed("bounced@example.de"))

    def test_a_user_without_delete_permission_is_refused(self):
        """django-object-actions never reads ``allowed_permissions``.

        The decorator accepts a ``permissions=`` argument and stores it, and
        nothing in the library ever looks at it — so a button that does not
        check for itself is a permission bypass. This asserts the explicit
        check in ``SuppressedAddressAdmin.unsuppress``.
        """
        self.member.is_staff = True
        self.member.save(update_fields=["is_staff"])
        self.client.force_login(self.member)

        response = self.client.post(self.action_url(self.suppressed))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(
            SuppressedAddress.objects.filter(pk=self.suppressed.pk).exists()
        )
