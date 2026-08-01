"""URLConf for the admin-only automation panel.

Mounted at ``admin/automation/`` from ``config/urls.py``, ahead of
``admin.site.urls`` — ``AdminSite.get_urls`` finishes with a catch-all, so
anything mounted after it is unreachable.

``admin.site.admin_view`` is applied here rather than as a decorator on the view
so the permission wrapper is visible at the routing layer, where a reviewer
looks for it.
"""

from __future__ import annotations

from django.contrib import admin
from django.urls import path

from apps.automation.admin import n8n_panel_view

app_name = "automation"

urlpatterns = [
    path(
        "",
        admin.site.admin_view(n8n_panel_view),
        name="n8n-panel",
    ),
]
