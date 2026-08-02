"""The findajob.pro admin site: branding plus a deliberate index order.

Why an ``AdminSite`` subclass and not "ordering hooks on each ModelAdmin"
-----------------------------------------------------------------------
There is no per-ModelAdmin hook for the index. ``AdminSite.get_app_list`` sorts
apps by ``AppConfig.verbose_name`` and models by ``verbose_name_plural``, full
stop — which is why the folk remedy is to prefix a model's verbose name with
``"a "`` / ``"b "`` to cheat the sort. That corrupts every label the operator
ever sees: the changelist heading, the breadcrumbs, the "Add <model>" button,
the delete-confirmation sentence. Overriding ``get_app_list`` once keeps the
labels honest and puts the ordering in a single readable place.

Default order, for reference, is alphabetical by app verbose name::

    Accounts & authentication · Authentication and Authorization ·
    Legal & consent · Tenancy

…which buries Tenancy, the app you reach for second in almost every support
question, at the bottom. The order below follows the path an operator actually
walks: **who is this person** → **which workspace are they in** → **what did
they agree to**, with Django's own plumbing last.

Coordination note
-----------------
``index_template`` is deliberately **not** set here. ``apps.automation``'s
``AppConfig.ready()`` claims that slot only when it is still empty
(``if not getattr(admin.site, "index_template", None)``), so setting it on this
class would silently drop the n8n card from the admin index. If this site ever
needs its own index template, it must extend whatever ``admin/index.html``
resolves to and include ``admin/_n8n_panel.html`` — that include is the agreed
hand-off point between the two owners.
"""

from __future__ import annotations

from django.contrib import admin
from django.utils.translation import gettext_lazy as _

#: App labels in the order they should appear on the index. Anything not listed
#: keeps Django's alphabetical placement, after these.
APP_ORDER: tuple[str, ...] = (
    "accounts",   # identity: who is this person
    "tenancy",    # workspace: where do they belong, what have they done
    "legal",      # evidence: what did they agree to, and against which text
    "automation", # operational surfaces (no models today; future-proof)
    "auth",       # Django's own groups/permissions
)

#: Model order *within* an app, by ``Model._meta.model_name``. Same rule:
#: unlisted models keep their alphabetical position, after the listed ones.
MODEL_ORDER: dict[str, tuple[str, ...]] = {
    # The account, then the credential it logs in with, then the reason its
    # mail stopped arriving — the exact sequence of a "cannot log in" ticket.
    "accounts": ("user", "emailotp", "suppressedaddress"),
    # The workspace, its members, then everything that happened in it.
    "tenancy": ("organization", "membership", "auditlog"),
    # The text first, because a consent record is meaningless until you can see
    # which document version its hash belongs to.
    "legal": ("legaldocument", "consentrecord"),
}


class FindajobAdminSite(admin.AdminSite):
    """findajob.pro's admin site.

    Installed by :class:`apps.common.admin_config.FindajobAdminConfig`, which
    replaces ``django.contrib.admin`` in ``INSTALLED_APPS``. The app *label*
    stays ``admin``, so templates, static files, URL names (``admin:index``, …)
    and ``django.contrib.admin.checks`` are all unaffected.
    """

    site_header = _("findajob.pro administration")
    site_title = _("findajob.pro admin")
    index_title = _("Operations")

    #: The "View site" link in the header. The product UI is a static page
    #: served by nginx at the same origin.
    site_url = "/index.html"

    def _app_rank(self, app: dict) -> tuple[int, str]:
        label = app.get("app_label", "")
        try:
            rank = APP_ORDER.index(label)
        except ValueError:
            rank = len(APP_ORDER)
        return rank, str(app.get("name", "")).lower()

    def _model_rank(self, app_label: str, model: dict) -> tuple[int, str]:
        order = MODEL_ORDER.get(app_label, ())
        model_class = model.get("model")
        name = model_class._meta.model_name if model_class is not None else ""
        try:
            rank = order.index(name)
        except ValueError:
            rank = len(order)
        return rank, str(model.get("name", ""))

    def get_app_list(self, request, app_label=None):
        """Django's list, re-sorted by :data:`APP_ORDER` / :data:`MODEL_ORDER`.

        ``super()`` still does the work that matters — permission filtering,
        URL reversing, the ``app_label`` narrowing used by the app-index page —
        so an app or model this module has never heard of still shows up, just
        alphabetically and after the ones it has.
        """
        app_list = super().get_app_list(request, app_label)
        app_list.sort(key=self._app_rank)
        for app in app_list:
            current_label = app.get("app_label", "")
            app["models"].sort(
                key=lambda model, label=current_label: self._model_rank(label, model)
            )
        return app_list

    def _fj_kpis(self) -> list[dict]:
        """Global platform counts for the index dashboard cards.

        Deliberately cross-tenant: this is the operator's console. Org-scoped
        models are read through the greppable ``.unscoped()``; the rest through
        their plain managers. Each count is independent so one broken app never
        blanks the whole row.
        """
        from django.utils import timezone

        from apps.accounts.models import EmailOTP, SuppressedAddress, User
        from apps.jobs.models import JobListing
        from apps.outbox.models import MailboxConnection
        from apps.tenancy.models import Organization

        today = timezone.localdate()
        return [
            {"key": "users", "label": _("Users"),
             "value": User.objects.count()},
            {"key": "organizations", "label": _("Organizations"),
             "value": Organization.objects.count()},
            {"key": "job_listings", "label": _("Job listings"),
             "value": JobListing.objects.count()},
            {"key": "otps_today", "label": _("OTP codes today"),
             "value": EmailOTP.objects.filter(created_at__date=today).count()},
            {"key": "mailboxes", "label": _("Mailbox connections"),
             "value": MailboxConnection.objects.unscoped().count()},
            {"key": "suppressed", "label": _("Suppressed addresses"),
             "value": SuppressedAddress.objects.count()},
        ]

    def index(self, request, extra_context=None):
        """Render the admin index with KPI context."""
        extra_context = {**(extra_context or {}), "fj_kpis": self._fj_kpis()}
        return super().index(request, extra_context=extra_context)

    def login(self, request, extra_context=None):
        """Throttle failed sign-ins before authenticating.

        Django ships no rate limiting on this form. Traefik basicAuth gates
        ``/admin/`` from the internet, but every container on the shared
        ``gethub_gethub-network`` reaches nginx — and therefore this view —
        without passing it. See :mod:`apps.common.admin_throttle`.
        """
        from apps.common.admin_throttle import enforce_admin_login_budget

        if request.method == "POST":
            enforce_admin_login_budget(request, request.POST.get("username", ""))
        return super().login(request, extra_context)
