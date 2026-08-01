"""The n8n panel's admin view.

**Why a plain view wrapped in ``admin.site.admin_view`` and not a ModelAdmin or
an AdminSite subclass:**

* A *ModelAdmin* needs a model. This app has none and wants none — inventing an
  unmanaged proxy model purely to hang a URL off it would put a fake entry in
  the admin app list, need a migration decision, and lie about what exists in
  the database.
* An *AdminSite subclass* means replacing ``django.contrib.admin`` in
  ``INSTALLED_APPS`` with a custom ``AdminConfig`` and re-pointing
  ``default_site``. That is a project-wide, single-owner change to the admin
  bootstrap — exactly the kind of thing two agents working the same admin at the
  same time must not both attempt.
* ``AdminSite.admin_view`` is the documented wrapper that ``ModelAdmin.get_urls``
  itself uses. It gives the identical guarantees with no ownership conflict:
  ``never_cache``, the ``has_permission`` check (active **and** staff), and a
  redirect to the admin login carrying ``?next=`` for anyone who fails it.

The route is mounted in ``config/urls.py`` *before* ``admin.site.urls``, because
``AdminSite.get_urls`` ends in a catch-all.
"""

from __future__ import annotations

from django.contrib import admin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _

from apps.automation import config
from apps.automation.client import get_client
from apps.automation.panel import build_panel


def n8n_panel_view(request: HttpRequest) -> HttpResponse:
    """``GET /admin/automation/`` — read-only view of the n8n instance.

    Wrapped by :func:`admin.site.admin_view` at URL-registration time, so by the
    time this runs the caller is an active staff user.
    """
    panel = build_panel(
        get_client(),
        workflow_limit=config.workflow_limit(),
        execution_limit=config.execution_limit(),
        execution_workflow_limit=config.execution_workflow_limit(),
        budget_seconds=config.panel_budget_seconds(),
    )

    context = {
        **admin.site.each_context(request),
        "title": _("Automation — n8n"),
        "panel": panel,
        "read_only_notice": _(
            "This panel is read-only. It issues GET requests to the n8n API and "
            "nothing else — no triggering, no activating, no editing."
        ),
    }
    return render(request, "admin/automation/n8n_panel.html", context)
