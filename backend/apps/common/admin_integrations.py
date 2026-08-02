"""Seams for the two third-party admin packages.

``apps.common.admin_support`` is model-free *and* dependency-free — every app
may import it, and it imports nothing but Django. This module is the opposite
end: it is the only place allowed to import ``import_export`` and
``django_object_actions``, so that "which packages does the admin depend on"
is answered by one grep rather than by reading eight ``admin.py`` files.

Both packages solve their problem by shipping a changelist or change-form
template, and both collide with something this project already ships. Neither
collision raises: ``change_list_template`` is a single string, so whichever
class the MRO reaches first wins and the loser silently does nothing. That is
the entire reason this module exists — the mixins below fix the ordering once,
by name, instead of leaving it to base-class order in a decorator line.

What is deliberately absent
---------------------------
``ImportMixin`` / ``ImportExportModelAdmin``. Both models wired for export here
are append-only ledgers: ``ConsentRecord.save`` refuses an UPDATE and
``AuditLog`` is the record of who did what. An admin-side import path into
either is a way to *write* evidence, which defeats the point of keeping it.
Export is a copy; import would be an edit.
"""

from __future__ import annotations

import logging

from django.apps import apps as django_apps
from django_object_actions import DjangoObjectActions
from import_export.admin import ExportActionMixin
from import_export.formats.base_formats import CSV, JSON
from import_export.resources import ModelResource

from apps.common.admin_support import CHANGE_LIST_NOTE_TEMPLATE

logger = logging.getLogger(__name__)

#: Export formats offered in the admin, replacing the library's default list.
#:
#: The default is "everything tablib can serialise", which here resolves to
#: CSV, TSV, JSON, YAML and HTML — the binary spreadsheet formats drop out on
#: their own because openpyxl is not installed. Two is enough: a spreadsheet
#: reads CSV and a script reads JSON. TSV is CSV with worse quoting, YAML needs
#: a parser nobody downstream has, and HTML is a *rendered* export — an
#: evidence table turned into markup a browser will interpret, which is a
#: strange thing to hand somebody as a record.
#:
#: Adding XLSX later is `pip install openpyxl` plus one entry here.
EXPORT_FORMATS = [CSV, JSON]

#: django-object-actions' changelist template. Named rather than imported so
#: that :class:`ObjectActionsAdminMixin` can hand it to the note template as a
#: parent; see ``AdminNoteMixin.admin_note_base_template``.
OBJECT_ACTIONS_CHANGE_LIST_TEMPLATE = "django_object_actions/change_list.html"


class EvidenceExportAdminMixin(ExportActionMixin):
    """Adds an "Export selected …" action to a read-only evidence changelist.

    Mix in **before** ``admin.ModelAdmin`` and after this project's own mixins::

        class ConsentRecordAdmin(
            EvidenceExportAdminMixin, ReadOnlyAdminMixin, admin.ModelAdmin
        ):
            resource_classes = [ConsentRecordResource]

    Three things are worth knowing about how this behaves here, because each
    one is a place the obvious expectation is wrong:

    **It exports what the changelist shows, not what the table holds.** The
    action receives the queryset the ``ChangeList`` built — filters, search and
    ``ModelAdmin.get_queryset`` all applied. That matters most for
    ``tenancy.AuditLog``, whose default manager raises ``OrgScopeError`` on an
    unscoped read: the export inherits ``UnguardedQuerySetMixin``'s queryset
    like every other admin read does, rather than reaching for
    ``Model.objects`` and blowing up.

    **The change-form export button is off.** ``ExportActionMixin`` normally
    adds one, and its handler is hard-coded to ``self.model.objects.filter(…)``
    — the *guarded* manager, which raises for exactly the model that most needs
    exporting. Rather than patch around it, the button is disabled and the
    changelist action (select rows → Export) is the single path.

    **It does not weaken the read-only contract.** The action needs no add,
    change or delete permission, so it coexists with ``ReadOnlyAdminMixin``;
    and because ``has_export_permission`` is left at its default, any staff
    user who can open the changelist can export it. That is the intended
    posture — they can already read every row, one page at a time. What it adds
    is *bulk*, which is why every completed export is recorded to
    ``tenancy.AuditLog`` by :meth:`_do_file_export` below.
    """

    #: Overrides ``BaseExportMixin.export_formats``, which is a property that
    #: reads ``settings.IMPORT_EXPORT_FORMATS``. A plain class attribute on a
    #: subclass shadows an inherited property, so this wins without needing the
    #: setting — see the note in ``config/settings/base.py`` for why the
    #: setting is not used.
    export_formats = EXPORT_FORMATS

    #: See the class docstring: the change-form button's handler bypasses
    #: ``get_queryset``.
    show_change_form_export = False

    #: ``AuditLog.action`` for a completed export.
    export_audit_action = "admin.export"

    def get_valid_export_item_pks(self, request):
        """The pks the export form will accept — read through ``get_queryset``.

        The library's implementation is ``self.model.objects.all()``, which on
        ``tenancy.AuditLog`` is the *guarded* manager and raises
        ``OrgScopeError`` the moment the form is instantiated. That happens
        only on the two-step path (select rows on the changelist → confirm),
        so it is invisible to a smoke test that merely loads the changelist and
        surfaces the first time somebody actually exports something.

        Overriding it also settles its ``DeprecationWarning``: the library
        wants this replaced by ``get_queryset``/``get_export_queryset``
        overrides, which is exactly what this delegates to.
        """
        return self.get_export_queryset(request).values_list("pk", flat=True)

    def _do_file_export(self, file_format, request, queryset, **kwargs):
        """Produce the file, then record that somebody took a copy.

        This is the library's single choke point for a *completed* export —
        both the standalone export page and the changelist action funnel
        through it, and it is not reached when the form fails validation. So
        one entry per file, not one per row and not one per page view.

        django-import-export's own ``IMPORT_EXPORT_SKIP_ADMIN_LOG`` does not
        cover this: it governs **imports** only, and nothing in the library
        writes a ``LogEntry`` on export. ``tenancy.AuditLog`` is the better
        home anyway — it is this project's append-only record, it scrubs its
        payload, and unlike ``LogEntry`` it does not have to pretend an export
        was an ADDITION or a CHANGE.

        Resolved through the app registry rather than imported, for the reason
        given in ``apps/accounts/admin.py``: ``apps.common`` is plumbing every
        app may depend on and none may be depended upon by. With ``tenancy``
        absent the export still works and the recording is skipped.
        """
        response = super()._do_file_export(file_format, request, queryset, **kwargs)
        self._record_export(request, queryset, file_format)
        return response

    def _record_export(self, request, queryset, file_format) -> None:
        try:
            audit_log = django_apps.get_model("tenancy", "AuditLog")
        except LookupError:  # pragma: no cover - tenancy is always installed
            return

        try:
            audit_log.objects.record(
                action=self.export_audit_action,
                actor=getattr(request, "user", None),
                target_type=self.model._meta.label_lower,
                payload={
                    "format": file_format.get_title(),
                    "rows": queryset.count(),
                    "model": self.model._meta.label,
                },
                ip=request.META.get("REMOTE_ADDR"),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
        except Exception:  # pragma: no cover - never fail an export over its log
            # The file has already been produced and is about to be returned.
            # Losing the audit row is bad; losing the export the operator asked
            # for, because writing the audit row failed, is worse and harder to
            # understand. Log it loudly instead.
            logger.exception(
                "failed to record %s export to the audit log", self.model._meta.label
            )


class ObjectActionsAdminMixin(DjangoObjectActions):
    """django-object-actions, composed with this project's changelist note.

    ``DjangoObjectActions`` sets ``change_list_template`` and
    ``change_form_template``; :class:`~apps.common.admin_support.AdminNoteMixin`
    sets ``change_list_template`` too. Only one value survives, and which one
    depends on the order of base classes in the ``class`` line — with no error
    either way. Whichever loses, its feature vanishes: either the operator note
    above the table, or the buttons.

    They are not actually in conflict. Both templates extend
    ``admin/change_list.html`` and override *different* blocks — ``search``
    for the note, ``object-tools-items`` for the buttons — so chaining them
    works. This class pins the chain: the note template wins
    ``change_list_template``, and it is told to extend django-object-actions'
    template rather than the stock one.

    Use it in place of ``DjangoObjectActions``, listed first::

        class SuppressedAddressAdmin(
            ObjectActionsAdminMixin, AdminNoteMixin, admin.ModelAdmin
        ):
            change_actions = ("unsuppress",)
    """

    change_list_template = CHANGE_LIST_NOTE_TEMPLATE
    admin_note_base_template = OBJECT_ACTIONS_CHANGE_LIST_TEMPLATE


__all__ = [
    "EXPORT_FORMATS",
    "EvidenceExportAdminMixin",
    "ModelResource",
    "ObjectActionsAdminMixin",
]
