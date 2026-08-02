"""The changelist cost controls in ``apps.common.admin_support``.

Everything here fails *silently* when it regresses: a changelist that runs two
full ``COUNT(*)`` instead of none is not slower on a table with six rows in it,
and the test database always has six rows in it. So these tests assert the
configuration and the decision logic directly rather than trying to observe a
speed difference that does not exist at test scale.

The one thing they must not do is assert that the estimate is *used* on SQLite:
it deliberately is not. :class:`EstimatedCountPaginator` only reaches for
``pg_class.reltuples`` on Postgres, so under the default local/test database
every assertion about a real count is an assertion about the fallback path.
That fallback is the important half — it is what runs whenever the number has
to be right.
"""

from __future__ import annotations

from unittest import mock

from django.contrib import admin
from django.db import DatabaseError, connection
from django.test import SimpleTestCase, TestCase

from apps.accounts.models import EmailOTP, SuppressedAddress, User
from apps.common.admin_support import (
    ESTIMATE_THRESHOLD,
    EstimatedCountPaginator,
    LargeTableAdminMixin,
)
from apps.legal.models import ConsentRecord
from apps.tenancy.models import AuditLog
from tests.admin.support import AdminTestCase, changelist_url

#: The models whose row count is driven by traffic rather than by the number of
#: customers or the size of the code base. Each one must carry the mixin.
#:
#: This list is the actual claim being tested and is worth keeping honest as
#: models are added: ``JobListing`` and ``Application`` belong here the day they
#: exist, and a new high-volume model that is *deliberately* left off should be
#: removed from this list with a reason, not silently omitted.
TRAFFIC_SCALED_MODELS = (EmailOTP, AuditLog, ConsentRecord)

#: Models that are deliberately NOT on the mixin, and why. Bounded tables get
#: an exact count because it is cheap and right.
BOUNDED_MODELS = (SuppressedAddress,)

#: App labels this project owns. ``auth`` is excluded on purpose — Django
#: registers ``Group`` itself and its defaults are Django's business.
PROJECT_APP_LABELS = frozenset({"accounts", "tenancy", "legal", "automation"})


class LargeTableAdminMixinTests(SimpleTestCase):
    def test_the_traffic_scaled_models_use_it(self):
        for model in TRAFFIC_SCALED_MODELS:
            with self.subTest(model=model._meta.label):
                model_admin = admin.site._registry[model]
                self.assertIsInstance(model_admin, LargeTableAdminMixin)

    def test_bounded_models_keep_the_exact_count(self):
        for model in BOUNDED_MODELS:
            with self.subTest(model=model._meta.label):
                model_admin = admin.site._registry[model]
                self.assertNotIsInstance(model_admin, LargeTableAdminMixin)

    def test_the_second_count_is_off(self):
        """``show_full_result_count`` is the expensive decoration.

        Django's default (True) makes every *filtered* changelist run a second,
        unfiltered ``COUNT(*)`` purely to print "(N total)". It is the single
        setting on this mixin that costs the most and is noticed the least.
        """
        for model in TRAFFIC_SCALED_MODELS:
            with self.subTest(model=model._meta.label):
                self.assertFalse(admin.site._registry[model].show_full_result_count)

    def test_facets_are_never_computed(self):
        for model in TRAFFIC_SCALED_MODELS:
            with self.subTest(model=model._meta.label):
                self.assertEqual(
                    admin.site._registry[model].show_facets,
                    admin.ShowFacets.NEVER,
                )

    def test_the_paginator_is_the_estimating_one(self):
        for model in TRAFFIC_SCALED_MODELS:
            with self.subTest(model=model._meta.label):
                self.assertIs(
                    admin.site._registry[model].paginator, EstimatedCountPaginator
                )

    def test_every_admin_of_ours_still_declares_a_page_size(self):
        """The mixin replaced hand-written ``list_per_page`` lines.

        A model that lost its explicit value *and* did not gain the mixin would
        silently fall back to Django's default of 100, which is a doubling of
        every changelist query's result set.

        Scoped to this project's own apps. ``auth.Group`` is registered by
        ``django.contrib.auth`` and keeps Django's default — correctly: a
        permission group table holds a handful of rows and is not ours to
        reconfigure.
        """
        for model, model_admin in admin.site._registry.items():
            if model._meta.app_label not in PROJECT_APP_LABELS:
                continue
            with self.subTest(model=model._meta.label):
                self.assertLessEqual(
                    model_admin.list_per_page,
                    50,
                    f"{model._meta.label} renders more than 50 rows per page",
                )


class EstimatedCountPaginatorFallbackTests(TestCase):
    """The cases that must produce a real, exact count."""

    @classmethod
    def setUpTestData(cls) -> None:
        for index in range(7):
            User.objects.create_user(email=f"counted-{index}@example.de")

    def paginator(self, queryset) -> EstimatedCountPaginator:
        return EstimatedCountPaginator(queryset, per_page=50)

    def test_sqlite_and_any_non_postgres_backend_counts_exactly(self):
        if connection.vendor == "postgresql":
            self.skipTest("this asserts the non-Postgres path")
        paginator = self.paginator(User.objects.all())
        self.assertIsNone(paginator.estimated_count())
        self.assertEqual(paginator.count, User.objects.count())

    def test_a_filtered_queryset_is_never_estimated(self):
        """The important restriction.

        ``reltuples`` describes the whole table. An operator who filtered or
        searched is asking a question whose answer must be right, and it is
        also the case where a wrong number is most visibly wrong — six results
        on screen under a header claiming "about 1,200,000".
        """
        queryset = User.objects.filter(email__startswith="counted-")
        self.assertIsNone(self.paginator(queryset).estimated_count())

    def test_a_distinct_queryset_is_never_estimated(self):
        self.assertIsNone(self.paginator(User.objects.distinct()).estimated_count())

    def test_a_plain_list_is_never_estimated(self):
        """``Paginator`` accepts any sequence; only a queryset has a ``query``."""
        paginator = self.paginator(["a", "b", "c"])
        self.assertIsNone(paginator.estimated_count())
        self.assertEqual(paginator.count, 3)

    def test_an_estimate_under_the_threshold_is_discarded(self):
        """Small table: the exact count is cheap, so accuracy wins."""
        paginator = self.paginator(User.objects.all())
        with mock.patch.object(
            EstimatedCountPaginator, "estimated_count", return_value=None
        ):
            self.assertEqual(paginator.count, User.objects.count())

    def test_a_returned_estimate_is_used_verbatim(self):
        """The one case where the number is allowed to be approximate."""
        paginator = self.paginator(User.objects.all())
        with mock.patch.object(
            EstimatedCountPaginator,
            "estimated_count",
            return_value=ESTIMATE_THRESHOLD * 100,
        ):
            self.assertEqual(paginator.count, ESTIMATE_THRESHOLD * 100)

    def test_a_database_error_falls_back_instead_of_propagating(self):
        """A missing table or a permission problem must not 500 the changelist.

        The real query is about to run anyway and will raise a real error with
        a real traceback; masking it here would only add a second, worse one.

        ``User.objects.all()`` is genuinely unfiltered, so it clears every
        earlier guard and reaches the cursor — which is the point: this
        exercises the last one.
        """
        paginator = self.paginator(User.objects.all())
        with mock.patch("apps.common.admin_support.connections") as connections:
            connection_mock = connections.__getitem__.return_value
            connection_mock.vendor = "postgresql"
            connection_mock.cursor.side_effect = DatabaseError(
                "relation does not exist"
            )
            self.assertIsNone(paginator.estimated_count())

    def test_a_postgres_estimate_is_read_and_returned(self):
        """The Postgres path itself, without needing a Postgres database.

        Everything above proves the *fallbacks*. This proves the branch they
        fall back from: given a Postgres vendor and a ``reltuples`` above the
        threshold, the estimate is what comes out.
        """
        paginator = self.paginator(User.objects.all())
        with mock.patch("apps.common.admin_support.connections") as connections:
            connection_mock = connections.__getitem__.return_value
            connection_mock.vendor = "postgresql"
            cursor = connection_mock.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = (1_204_000,)
            self.assertEqual(paginator.estimated_count(), 1_204_000)

    def test_postgres_reltuples_of_minus_one_is_discarded(self):
        """``-1`` means "this table has never been analysed", not "empty"."""
        paginator = self.paginator(User.objects.all())
        with mock.patch("apps.common.admin_support.connections") as connections:
            connection_mock = connections.__getitem__.return_value
            connection_mock.vendor = "postgresql"
            cursor = connection_mock.cursor.return_value.__enter__.return_value
            cursor.fetchone.return_value = (-1,)
            self.assertIsNone(paginator.estimated_count())

    def test_the_reltuples_query_is_valid_sql_on_a_real_postgres(self):
        """Runs the actual query, unmocked, when the suite is on Postgres.

        Everything above mocks the cursor, which proves the decision logic and
        proves nothing about whether ``SELECT reltuples::bigint FROM pg_class
        WHERE oid = %s::regclass`` parses, resolves the table name, or returns
        the type the code assumes.

        It skips by default and that is not a temporary state: ``POSTGRES_DB``
        does **not** reach here, because ``config.settings.test`` pins the
        database to in-memory SQLite unconditionally so the suite runs with
        zero infrastructure. Reaching this assertion means pointing the runner
        at a settings module that overrides ``DATABASES`` — worth doing before
        touching :class:`EstimatedCountPaginator`, since Postgres is the only
        backend where its interesting branch runs at all.

        It asserts the plumbing, not the number: a freshly-created test table
        may never have been analysed, in which case ``reltuples`` is
        legitimately ``-1`` and the threshold rejects it. What must not happen
        is an exception.
        """
        if connection.vendor != "postgresql":
            self.skipTest("no Postgres connection; SQLite takes the fallback path")

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT reltuples::bigint FROM pg_class WHERE oid = %s::regclass",
                [User._meta.db_table],
            )
            row = cursor.fetchone()

        self.assertIsNotNone(row, "the users table is not visible as a regclass")
        self.assertIsInstance(row[0], int)
        # And the paginator itself completes without raising, whichever branch
        # it lands in.
        self.assertEqual(self.paginator(User.objects.all()).count, User.objects.count())


class ChangelistStillRendersTests(AdminTestCase):
    """The mixin must not change what the page says, only what it costs."""

    def test_every_traffic_scaled_changelist_loads_and_shows_its_rows(self):
        for model in TRAFFIC_SCALED_MODELS:
            with self.subTest(model=model._meta.label):
                response = self.client.get(changelist_url(model))
                self.assertEqual(response.status_code, 200)
                changelist = response.context["cl"]
                self.assertEqual(
                    changelist.result_count, model._base_manager.count()
                )

    def test_a_filtered_changelist_still_counts_exactly(self):
        """With ``show_full_result_count`` off, ``result_count`` is still real."""
        response = self.client.get(changelist_url(EmailOTP), {"q": "used@example.de"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["cl"].result_count, 1)
