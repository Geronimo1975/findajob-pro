"""One smoke test per registered ModelAdmin.

A ``list_display`` entry naming a field that was renamed, a ``list_filter`` on a
path that no longer resolves, a ``readonly_fields`` callable that raises on a
row with a null FK — every one of these is a 500 on exactly one page and is
invisible to the API suite. Iterating ``admin.site._registry`` means a model
registered tomorrow is covered the day it is registered, with no test to write.

The coverage per model is deliberately four requests, because they fail for
different reasons:

* the **changelist**, which renders ``list_display``, every ``list_filter``
  (including each filter's ``lookups()``), ``date_hierarchy`` and the action
  form;
* the changelist **with a search term**, which compiles ``search_fields`` into
  joins — the place a bad related lookup surfaces;
* the **change form** of a real row, which renders ``fieldsets`` and every
  ``readonly_fields`` callable against real data;
* the **add form**, or a 403 where adding is closed.
"""

from __future__ import annotations

from django.contrib import admin

from tests.admin.support import (
    AdminTestCase,
    add_url,
    change_url,
    changelist_url,
    first_row,
)


def registered_models():
    return sorted(admin.site._registry, key=lambda model: model._meta.label)


class AdminIndexTests(AdminTestCase):
    def test_admin_index_renders(self):
        response = self.client.get("/admin/")
        self.assertEqual(response.status_code, 200)

    def test_every_registered_model_has_an_app_index_entry(self):
        """A model registered but unreachable from the index is invisible."""
        request = self.client.get("/admin/").wsgi_request
        listed = {
            entry["model"]._meta.label
            for app in admin.site.get_app_list(request)
            for entry in app["models"]
        }
        for model in registered_models():
            with self.subTest(model=model._meta.label):
                self.assertIn(model._meta.label, listed)


class ChangelistSmokeTests(AdminTestCase):
    def test_every_changelist_loads(self):
        for model in registered_models():
            with self.subTest(model=model._meta.label):
                response = self.client.get(changelist_url(model))
                self.assertEqual(
                    response.status_code, 200, f"{model._meta.label} changelist"
                )

    def test_every_changelist_search_compiles(self):
        for model in registered_models():
            model_admin = admin.site._registry[model]
            if not model_admin.search_fields:
                continue
            with self.subTest(model=model._meta.label):
                response = self.client.get(changelist_url(model), {"q": "example"})
                self.assertEqual(
                    response.status_code, 200, f"{model._meta.label} search"
                )

    def test_every_changelist_sorts_by_every_sortable_column(self):
        """``admin_order_field`` on a computed column must name a real expression."""
        for model in registered_models():
            model_admin = admin.site._registry[model]
            for index in range(len(model_admin.list_display)):
                with self.subTest(model=model._meta.label, column=index):
                    response = self.client.get(
                        changelist_url(model), {"o": str(index + 1)}
                    )
                    self.assertEqual(response.status_code, 200)


class ChangeFormSmokeTests(AdminTestCase):
    def test_every_change_form_loads(self):
        for model in registered_models():
            row = first_row(model)
            with self.subTest(model=model._meta.label):
                self.assertIsNotNone(
                    row, f"no fixture row for {model._meta.label}; add one to support.py"
                )
                response = self.client.get(change_url(row))
                self.assertEqual(
                    response.status_code, 200, f"{model._meta.label} change form"
                )

    def test_add_form_matches_the_declared_add_permission(self):
        request = self.client.get("/admin/").wsgi_request
        for model in registered_models():
            model_admin = admin.site._registry[model]
            allowed = model_admin.has_add_permission(request)
            with self.subTest(model=model._meta.label, allowed=allowed):
                response = self.client.get(add_url(model))
                if allowed:
                    self.assertEqual(response.status_code, 200)
                else:
                    self.assertEqual(response.status_code, 403)


class OrgGuardTests(AdminTestCase):
    """The regression that would break every tenancy page at once.

    ``Membership`` and ``AuditLog`` default to a manager that raises
    ``OrgScopeError`` on an unscoped read. Losing the ``all_objects`` escape
    hatch turns each of these pages into a 500 — and because the guard raises a
    ``RuntimeError`` subclass rather than returning wrong data, it fails loudly
    but only when somebody opens the page.
    """

    GUARDED = ("tenancy.Membership", "tenancy.AuditLog")

    def test_guarded_models_read_through_all_objects(self):
        from apps.tenancy.managers import OrgScopeError

        for model in registered_models():
            if model._meta.label not in self.GUARDED:
                continue
            model_admin = admin.site._registry[model]
            request = self.client.get("/admin/").wsgi_request
            with self.subTest(model=model._meta.label):
                try:
                    self.assertGreaterEqual(
                        len(list(model_admin.get_queryset(request))), 1
                    )
                except OrgScopeError as exc:  # pragma: no cover - regression guard
                    self.fail(f"{model._meta.label} admin lost its escape hatch: {exc}")

    def test_organization_change_form_renders_its_membership_inline(self):
        """The inline reads memberships, which is the guarded reverse relation."""
        response = self.client.get(change_url(self.org))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.member.email)
