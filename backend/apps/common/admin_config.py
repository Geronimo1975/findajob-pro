"""``AppConfig`` that installs :class:`~apps.common.admin_site.FindajobAdminSite`.

``INSTALLED_APPS`` lists ``"apps.common.admin_config.FindajobAdminConfig"`` in
place of ``"django.contrib.admin"``. ``AdminConfig.name`` is still
``"django.contrib.admin"``, so:

* the app **label** stays ``admin`` — ``admin:index``, ``admin/base_site.html``
  and ``/static/admin/…`` are untouched;
* ``django.contrib.admin.checks.check_dependencies`` still fires
  (``apps.is_installed("django.contrib.admin")`` matches on ``name``);
* ``ready()`` still runs ``autodiscover()``, because this subclasses
  ``AdminConfig`` and not ``SimpleAdminConfig``.

**This class must not live in** ``apps/common/apps.py``. Django resolves the
plain ``"apps.common"`` entry by scanning that module with
``inspect.getmembers`` and rejecting the app outright when more than one
candidate declares ``default = True`` — and ``AdminConfig`` sets ``default =
True``, so both this class *and* the imported ``AdminConfig`` symbol would
count. The failure is a hard ``RuntimeError`` at startup, not a subtle one, but
it is entirely avoidable by keeping it in its own module.
"""

from __future__ import annotations

from django.contrib.admin.apps import AdminConfig


class FindajobAdminConfig(AdminConfig):
    default_site = "apps.common.admin_site.FindajobAdminSite"
