"""Branding and index order.

Both are the kind of thing that silently reverts: ``INSTALLED_APPS`` losing the
custom ``AdminConfig`` entry leaves a perfectly working admin that is simply
Django's, with Django's alphabetical order and Django's "Django administration"
header. Nothing errors. These tests are the only thing that would notice.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import admin
from django.test import SimpleTestCase

from apps.common.admin_site import APP_ORDER, MODEL_ORDER, FindajobAdminSite
from tests.admin.support import AdminTestCase


class AdminSiteInstallationTests(SimpleTestCase):
    def test_installed_apps_points_at_the_custom_admin_config(self):
        self.assertIn(
            "apps.common.admin_config.FindajobAdminConfig", settings.INSTALLED_APPS
        )
        self.assertNotIn("django.contrib.admin", settings.INSTALLED_APPS)

    def test_the_default_site_is_ours(self):
        # `admin.site` is a LazyObject wrapping the real instance.
        self.assertIsInstance(admin.site._wrapped, FindajobAdminSite)

    def test_the_app_label_is_still_admin(self):
        """Templates, static files and every ``admin:*`` URL name depend on it."""
        from django.apps import apps as django_apps

        config = django_apps.get_app_config("admin")
        self.assertEqual(config.name, "django.contrib.admin")

    def test_branding(self):
        self.assertEqual(str(admin.site.site_header), "findajob.pro administration")
        self.assertEqual(str(admin.site.site_title), "findajob.pro admin")
        self.assertEqual(str(admin.site.index_title), "Operations")

    def test_the_index_template_slot_is_left_for_apps_automation(self):
        """Coordination contract, not decoration.

        ``apps.automation``'s ``ready()`` claims ``index_template`` only when it
        is still empty. Setting it on the site *class* would silently drop the
        n8n card from the admin index.
        """
        self.assertIsNone(FindajobAdminSite.index_template)


class AppListOrderTests(AdminTestCase):
    def app_list(self):
        request = self.client.get("/admin/").wsgi_request
        return admin.site.get_app_list(request)

    def test_apps_follow_the_declared_order(self):
        labels = [app["app_label"] for app in self.app_list()]
        declared = [label for label in APP_ORDER if label in labels]
        self.assertEqual(labels[: len(declared)], declared)

    def test_identity_comes_before_tenancy_which_comes_before_legal(self):
        """Alphabetically this would be accounts, auth, legal, tenancy."""
        labels = [app["app_label"] for app in self.app_list()]
        self.assertLess(labels.index("accounts"), labels.index("tenancy"))
        self.assertLess(labels.index("tenancy"), labels.index("legal"))
        self.assertLess(labels.index("legal"), labels.index("auth"))

    def test_models_follow_the_declared_order_within_each_app(self):
        for app in self.app_list():
            declared = MODEL_ORDER.get(app["app_label"])
            if not declared:
                continue
            actual = [entry["model"]._meta.model_name for entry in app["models"]]
            with self.subTest(app=app["app_label"]):
                self.assertEqual(actual, list(declared))

    def test_the_order_is_not_alphabetical(self):
        """Guards against the whole override silently disappearing."""
        for app in self.app_list():
            if app["app_label"] != "accounts":
                continue
            names = [entry["name"] for entry in app["models"]]
            self.assertNotEqual(names, sorted(names))
            return
        self.fail("accounts app missing from the admin index")

    def test_an_unlisted_app_still_appears(self):
        """Registering a model in a new app must not require editing APP_ORDER."""
        labels = {app["app_label"] for app in self.app_list()}
        self.assertIn("auth", labels)

    def test_app_index_page_uses_the_same_ordering(self):
        response = self.client.get("/admin/accounts/")
        self.assertEqual(response.status_code, 200)
        names = [
            entry["model"]._meta.model_name
            for app in response.context["app_list"]
            for entry in app["models"]
        ]
        self.assertEqual(names, list(MODEL_ORDER["accounts"]))

    def test_the_index_renders_the_declared_order_in_the_html(self):
        html = self.client.get("/admin/").content.decode()
        positions = [
            html.index('href="/admin/accounts/user/"'),
            html.index('href="/admin/accounts/emailotp/"'),
            html.index('href="/admin/tenancy/organization/"'),
            html.index('href="/admin/legal/legaldocument/"'),
        ]
        self.assertEqual(positions, sorted(positions))
