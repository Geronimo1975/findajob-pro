"""Shared Django-admin building blocks.

Five problems recur across the eight ModelAdmins this project registers, and
every one of them fails *silently* when each module solves it on its own:

1. **Tables that must not be writable.** ``LegalDocument``, ``ConsentRecord``
   and ``AuditLog`` are evidence, not data entry. Two of them refuse an UPDATE
   at the model layer already, and offering a form that always raises is worse
   than offering none — so :class:`ReadOnlyAdminMixin` turns the whole
   add/change/delete triple off in one place instead of three.

2. **Managers that raise on an unscoped read.** ``tenancy``'s default managers
   raise ``OrgScopeError`` rather than let a forgotten filter leak one tenant
   into another's response, and Django admin is one of the four blessed escape
   hatches. :class:`UnguardedQuerySetMixin` makes that escape explicit and
   greppable, and — unlike a bare ``get_queryset`` override — it still applies
   the ModelAdmin's own ``ordering``, which the hand-written overrides it
   replaces silently dropped.

3. **Free-text columns used as filters.** Django's default filter for an
   unconstrained ``CharField`` is ``AllValuesFieldListFilter``, which runs an
   unbounded ``SELECT DISTINCT`` on *every* changelist render. On
   ``AuditLog.action`` that is a full index scan that grows without limit.
   :func:`distinct_values_filter` caps it.

4. **Values that must be identifiable without being rendered verbatim.**
   :func:`short_digest` gives a stable handle for a row whose stored value is
   credential-shaped — see the honesty note in its docstring about exactly how
   much that does and does not buy.

5. **Changelists that pay for a full ``COUNT(*)`` on every render.** Django
   issues *two* of them per page on a filtered changelist — one for the
   paginator, one for ``show_full_result_count`` — and on Postgres a bare
   ``COUNT(*)`` is a sequential scan whose cost grows with the table.
   :class:`LargeTableAdminMixin` turns the second one off and swaps the first
   for :class:`EstimatedCountPaginator`.

Nothing here imports a model. It is deliberately model-free so that
``apps.common`` keeps its position as plumbing every app may depend on and none
of them may be depended upon by.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from django.contrib import admin
from django.core.paginator import Paginator
from django.db import DatabaseError, connections
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.safestring import SafeString

#: Changelist template that renders ``ModelAdmin.admin_note`` above the table.
#: It extends ``admin/change_list.html`` rather than replacing it, so a theme
#: override of that name keeps working.
CHANGE_LIST_NOTE_TEMPLATE = "admin/findajob/change_list_note.html"

#: Longest pretty-printed JSON blob rendered into a change form. Audit payloads
#: are small and scrubbed, but ``payload`` is a ``JSONField`` with no size limit
#: and one runaway row must not produce a megabyte of HTML.
MAX_JSON_PREVIEW_CHARS = 20_000

#: Hex characters in a :func:`short_digest`. Eight is enough to tell two rows
#: apart on screen and short enough to be quotable in a ticket.
DIGEST_LENGTH = 8

#: Placeholder for an empty computed column. Matches Django's own convention of
#: never rendering a bare blank cell.
EMPTY = "—"

#: Below this many rows an exact ``COUNT(*)`` is cheap enough that
#: :class:`EstimatedCountPaginator` should not trade accuracy for it. Every
#: table in the project is far under it today; ``JobListing`` will not be.
ESTIMATE_THRESHOLD = 10_000

#: Rows per changelist page. Matches what every ModelAdmin here already sets by
#: hand; :class:`LargeTableAdminMixin` exists so the next one does not have to.
LIST_PER_PAGE = 50

#: Ceiling on "Show all". Django's default is 200 and there is no reason to
#: raise it: the link exists to escape pagination on a small table, not to
#: render a hundred thousand rows into one document.
LIST_MAX_SHOW_ALL = 200


def short_digest(value: str | None, *, length: int = DIGEST_LENGTH) -> str:
    """A stable, short handle for ``value`` — **not** a redaction.

    Returns the first ``length`` hex characters of ``sha256(value)``. Because
    it is a digest *of* the stored value rather than a prefix of it, pasting
    one into a ticket or a screenshot never hands anybody a substring they can
    compare directly against the database column.

    Be honest about the limit: anyone holding ``SECRET_KEY`` can still
    enumerate a six-digit OTP keyspace, hash each candidate and match this
    fingerprint. That is unavoidable for *any* deterministic function of the
    stored hash, and it is not the threat this defends against. The threat is
    the ordinary one — a credential-shaped blob rendered on a screen, copied
    into a chat window, and living there forever.
    """
    text = (value or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def pretty_json(value: Any, *, limit: int = MAX_JSON_PREVIEW_CHARS) -> SafeString | str:
    """Render ``value`` as indented, escaped, scrollable JSON.

    ``format_html`` escapes the serialised text, so a payload containing markup
    cannot inject anything into the admin page. ``default=str`` keeps a stray
    ``datetime`` or ``Decimal`` from turning a change form into a 500.
    """
    if value in (None, "", {}, [], ()):
        return EMPTY
    try:
        text = json.dumps(
            value, indent=2, sort_keys=True, ensure_ascii=False, default=str
        )
    except (TypeError, ValueError):  # pragma: no cover - defensive
        text = str(value)

    suffix = ""
    if len(text) > limit:
        text = text[:limit]
        suffix = f"\n… truncated at {limit} characters"

    return format_html(
        '<pre style="margin:0;padding:.6rem .8rem;max-height:26rem;overflow:auto;'
        "white-space:pre-wrap;word-break:break-word;"
        "background:var(--darkened-bg, #f8f8f8);color:var(--body-fg, #333);"
        'border:1px solid var(--hairline-color, #e8e8e8);border-radius:4px">{}{}</pre>',
        text,
        suffix,
    )


def scrollable_block(inner: SafeString, *, max_height: str = "32rem") -> SafeString:
    """Wrap already-safe markup in a bounded, scrolling container."""
    return format_html(
        '<div style="max-height:{};overflow:auto;padding:.6rem .8rem;'
        "background:var(--darkened-bg, #f8f8f8);"
        'border:1px solid var(--hairline-color, #e8e8e8);border-radius:4px">{}</div>',
        max_height,
        inner,
    )


def distinct_values_filter(
    field_name: str,
    title,
    *,
    limit: int = 50,
    parameter_name: str | None = None,
):
    """Build a bounded "distinct values of this column" list filter.

    Django's ``AllValuesFieldListFilter`` is the default for a plain
    ``CharField`` and issues an unbounded ``SELECT DISTINCT``. This variant
    slices the result, so the cost of rendering the filter sidebar stays flat as
    the table grows. Values beyond ``limit`` are unreachable through the sidebar
    but remain reachable through ``search_fields`` — the right trade for a
    column like ``AuditLog.action`` whose vocabulary grows with the code, not
    with traffic.

    The choices come from ``model_admin.get_queryset(request)``, so a model
    whose default manager refuses an unscoped read is read through whatever
    unguarded manager that ModelAdmin already uses.
    """

    class _DistinctValuesFilter(admin.SimpleListFilter):
        def lookups(self, request, model_admin):
            queryset = model_admin.get_queryset(request)
            values = (
                queryset.order_by(field_name)
                .values_list(field_name, flat=True)
                .distinct()[:limit]
            )
            return [(value, value) for value in values if value]

        def queryset(self, request, queryset):
            value = self.value()
            if value:
                return queryset.filter(**{field_name: value})
            return queryset

    _DistinctValuesFilter.title = title
    _DistinctValuesFilter.parameter_name = parameter_name or field_name
    _DistinctValuesFilter.__name__ = f"{field_name.title().replace('_', '')}Filter"
    return _DistinctValuesFilter


class EstimatedCountPaginator(Paginator):
    """Paginator that stops paying for ``COUNT(*)`` on a whole large table.

    The problem is specific and worth stating precisely, because the usual
    "just cache the count" advice fixes the wrong half of it. Rendering a
    changelist needs a row count to draw the pager. On Postgres,
    ``SELECT COUNT(*) FROM t`` has no shortcut — no index-only path, no stored
    counter — so it reads every live tuple. At a few thousand rows that is
    noise. At ``JobListing`` scale it is the single slowest thing on the page,
    and it is paid again on every sort, every page turn and every filter.

    So this substitutes Postgres's own estimate (``pg_class.reltuples``, kept
    up to date by autovacuum) **only where the estimate is both available and
    honest**, and falls back to the exact count everywhere else:

    * **Not Postgres** → exact. Local development is SQLite; there is no
      estimate to read and the tables are tiny anyway.
    * **The query has a WHERE/DISTINCT/GROUP BY** → exact. ``reltuples`` counts
      the *table*, not a filtered subset, and a searched or filtered changelist
      is the case where a wrong number is most obviously wrong. This is the
      important restriction: an operator who filters gets a real answer, and
      only the unfiltered "open the whole table" page — the slow one — is
      estimated.
    * **The estimate is negative or under** :data:`ESTIMATE_THRESHOLD` →
      exact. Postgres writes ``-1`` for a table it has never analysed, and
      under the threshold the exact count is cheap enough that accuracy wins.
    * **The query fails** (missing table, permission, connection) → exact, and
      let the real query raise the real error rather than masking it here.

    The trade is deliberate and one-directional: the pager on one unfiltered
    page may say "about 1,204,000" when the truth is 1,203,847. Nothing in this
    project makes a decision from that number — it draws page links.
    """

    #: Rows below which the exact count is taken even on Postgres.
    estimate_threshold = ESTIMATE_THRESHOLD

    @cached_property
    def count(self) -> int:
        estimate = self.estimated_count()
        return super().count if estimate is None else estimate

    def estimated_count(self) -> int | None:
        """The planner's row estimate, or ``None`` to mean "count it properly"."""
        queryset = self.object_list
        query = getattr(queryset, "query", None)
        if query is None:  # a plain list — nothing to estimate
            return None

        # Any narrowing at all disqualifies the table-level estimate.
        if (
            query.where
            or query.distinct
            or query.group_by
            or getattr(query, "having", None)
            or query.combinator
        ):
            return None

        connection = connections[queryset.db]
        if connection.vendor != "postgresql":
            return None

        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass",
                    [queryset.model._meta.db_table],
                )
                row = cursor.fetchone()
        except DatabaseError:
            return None

        if not row or row[0] is None:
            return None

        estimate = int(row[0])
        if estimate < self.estimate_threshold:
            return None
        return estimate


class LargeTableAdminMixin:
    """Changelist defaults for a table that grows with traffic, not with code.

    Four settings, each closing a cost that is invisible until the table is
    big and then dominates the page:

    ``show_full_result_count = False``
        Django's default is ``True``, which means a *filtered* changelist runs
        a second, unfiltered ``COUNT(*)`` purely to render the "(N total)"
        hint next to the result count. It is the most expensive decoration in
        the admin. Off, the header reads "N results" and nothing else changes.

    ``paginator = EstimatedCountPaginator``
        See that class — the first count, made cheap where it can be made
        cheap honestly.

    ``show_facets = ShowFacets.NEVER``
        Facet counts add one aggregate per filter choice in the sidebar.
        Django 5.0+ already defers them behind ``?_facets=`` rather than
        running them by default, so this only closes the manual escape hatch —
        worth doing on a table where an operator clicking it would stall the
        page for everybody sharing the database.

    ``list_per_page`` / ``list_max_show_all``
        The numbers every ModelAdmin in this project already sets by hand.

    It sets no ``list_select_related``: the right value is per-model (the FKs
    that ``list_display`` actually renders) and a blanket ``select_related()``
    with no arguments — which is what Django falls back to when it spots a
    related field in ``list_display`` — joins *every* nullable FK, which on a
    wide model is slower than the N+1 it replaces. ``tests.admin`` asserts the
    explicit value instead.
    """

    paginator = EstimatedCountPaginator
    show_full_result_count = False
    show_facets = admin.ShowFacets.NEVER
    list_per_page = LIST_PER_PAGE
    list_max_show_all = LIST_MAX_SHOW_ALL


class AdminNoteMixin:
    """Renders ``admin_note`` above the changelist table.

    A model whose write paths are deliberately closed needs to say so where the
    person looking for the missing "Add" button is standing. A docstring in
    ``admin.py`` does not qualify.
    """

    #: Shown above the result list. Plain text; it is autoescaped by the
    #: template, so translations may not carry markup.
    admin_note = ""

    change_list_template = CHANGE_LIST_NOTE_TEMPLATE

    #: What :data:`CHANGE_LIST_NOTE_TEMPLATE` extends.
    #:
    #: ``change_list_template`` is a single string, so two mixins that each
    #: want their own changelist template cannot both win — and MRO order
    #: decides silently, with no error and no log line. Both templates in play
    #: here override *different* blocks of ``admin/change_list.html``
    #: (``search`` here, ``object-tools-items`` in django-object-actions), so
    #: they compose perfectly well as long as one extends the other. This is
    #: the knob that says which: point it at the other package's template and
    #: the note template chains onto it instead of replacing it.
    #:
    #: :class:`apps.common.admin_integrations.ObjectActionsAdminMixin` is the
    #: pre-wired combination; set this by hand only for a third one.
    admin_note_base_template = "admin/change_list.html"

    def changelist_view(self, request, extra_context=None):
        return super().changelist_view(
            request,
            {
                **(extra_context or {}),
                "admin_note_base_template": self.admin_note_base_template,
            },
        )


class ReadOnlyAdminMixin(AdminNoteMixin):
    """Look, search, filter — never write.

    Django removes ``delete_selected`` from the action list by itself once
    :meth:`has_delete_permission` is False (the action declares
    ``allowed_permissions = ("delete",)``), and renders the change form in view
    mode once :meth:`has_change_permission` is False, so this mixin needs to
    override nothing else.

    ``get_readonly_fields`` falls back to *every concrete field* when a subclass
    declares none, which means a field added to the model later is read-only
    from the moment it exists rather than from the moment somebody remembers to
    list it.
    """

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_readonly_fields(self, request, obj=None):
        declared = tuple(super().get_readonly_fields(request, obj))
        if declared:
            return declared
        return tuple(field.name for field in self.model._meta.fields)


class UnguardedQuerySetMixin:
    """Read through the model's *unguarded* manager.

    ``tenancy.Membership`` and ``tenancy.AuditLog`` set
    ``default_manager_name = "objects"`` on a manager that raises
    ``OrgScopeError`` when a queryset is evaluated without ``.for_org()`` /
    ``.for_user()``. Django admin is one of the four documented escape hatches
    (with migrations, management commands and tests), and this is where that
    exemption is taken — once, by name, instead of in every ``get_queryset``.

    Mirrors ``ModelAdmin.get_queryset`` exactly apart from the manager: the
    ModelAdmin's ``ordering`` is applied, which the hand-rolled overrides this
    replaces used to drop on the floor (leaving only ``Meta.ordering``).

    Works for ``InlineModelAdmin`` too — ``get_ordering`` lives on
    ``BaseModelAdmin``, which both inherit.
    """

    #: Name of the plain ``models.Manager`` attribute that bypasses the guard.
    unguarded_manager_name = "all_objects"

    def get_queryset(self, request):
        manager = getattr(self.model, self.unguarded_manager_name)
        queryset = manager.get_queryset()
        ordering = self.get_ordering(request)
        if ordering:
            queryset = queryset.order_by(*ordering)
        return queryset
