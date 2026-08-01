"""AppConfig for the shared plumbing app.

Its only job beyond namespacing is to import ``config.checks`` so the
``django.core.checks`` registrations actually happen. Importing a checks module
at settings-import time is not possible (the app registry is not ready yet), so
``ready()`` is the correct and only hook.
"""

from __future__ import annotations

from django.apps import AppConfig


class CommonConfig(AppConfig):
    name = "apps.common"
    label = "common"
    verbose_name = "Common"

    def ready(self) -> None:
        # Registers findajob.E001..E006 / W001 with django.core.checks.
        # Imported for the side effect; noqa because it is otherwise unused.
        from config import checks  # noqa: F401

        # Connects the user_login_failed receiver that feeds the admin-login
        # budget. Must happen at ready() time: a receiver defined in a module
        # nobody imports is a rate limiter that never counts anything.
        from apps.common import admin_throttle  # noqa: F401
