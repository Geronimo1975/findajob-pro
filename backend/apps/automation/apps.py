"""AppConfig for the automation panel.

Besides namespacing, ``ready()`` does one thing: point the admin index at a
template that appends the n8n card. It is a *wrapper* around
``admin/index.html`` rather than a replacement for it, so a theme override of
``admin/index.html`` keeps working — the wrapper extends whatever that name
resolves to at render time.
"""

from __future__ import annotations

from django.apps import AppConfig


class AutomationConfig(AppConfig):
    name = "apps.automation"
    label = "automation"
    verbose_name = "Automation (n8n)"

    def ready(self) -> None:
        from django.contrib import admin

        # Only claim the slot if nobody else has. A theme app that sets its own
        # index_template wins, and it is then that template's job to include
        # "admin/_n8n_panel.html" — the single agreed coordination point.
        if not getattr(admin.site, "index_template", None):
            admin.site.index_template = "admin/findajob_index.html"
