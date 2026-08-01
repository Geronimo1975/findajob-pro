"""``/admin/automation/`` — access control and rendering.

Two properties matter here and are asserted independently:

1. **Access.** Anonymous and non-staff users must never see it. The view is
   wrapped in ``admin.site.admin_view``, so failing that check is a redirect to
   the admin login, never a 200.
2. **It cannot 500.** Every n8n failure mode has to render as a page with an
   explanation. A panel that crashes when the automation host is down is worse
   than no panel.
"""

from __future__ import annotations

import socket
import urllib.error

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from tests.automation.support import (
    EMPTY_EXECUTIONS_PAYLOAD,
    EXECUTIONS_PAYLOAD,
    WORKFLOWS_PAYLOAD,
    FakeResponse,
    patched_urlopen,
    route_by_path,
)
from tests.support import make_user

PANEL_URL = "/admin/automation/"

N8N_CONFIGURED = dict(
    N8N_URL="https://n8n.example.test",
    N8N_API_TOKEN="test-token",
    N8N_TIMEOUT=5,
    N8N_CACHE_TTL=60,
    N8N_CACHE_ERROR_TTL=15,
)


def _happy_path():
    """urlopen side effect covering both endpoints the panel calls."""
    return route_by_path(
        **{
            "/workflows": WORKFLOWS_PAYLOAD,
            "/executions": EXECUTIONS_PAYLOAD,
        }
    )


class PanelTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

@override_settings(**N8N_CONFIGURED)
class AccessControlTests(PanelTestCase):
    def test_url_reverses_under_the_expected_path(self):
        self.assertEqual(reverse("automation:n8n-panel"), PANEL_URL)

    def test_anonymous_user_is_redirected_to_the_admin_login(self):
        with patched_urlopen(AssertionError("must not be called")) as patched:
            response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        patched.assert_not_called()

    def test_non_staff_user_is_redirected_and_sees_nothing(self):
        user = make_user()
        user.set_password("a-very-long-test-password")
        user.save()
        self.client.force_login(user)

        with patched_urlopen(AssertionError("must not be called")) as patched:
            response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])
        # The n8n API must not be touched on behalf of someone who cannot see it.
        patched.assert_not_called()

    def test_inactive_staff_user_is_refused(self):
        user = make_user()
        user.is_staff = True
        user.is_active = False
        user.save()
        self.client.force_login(user)

        response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 302)

    def test_staff_user_may_view_it(self):
        user = make_user()
        user.is_staff = True
        user.save()
        self.client.force_login(user)

        with patched_urlopen(_happy_path()):
            response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 200)

    def test_response_is_not_cacheable(self):
        """``admin_view`` wraps in ``never_cache`` — an admin page must not be
        stored by an intermediary."""
        user = make_user()
        user.is_superuser = user.is_staff = True
        user.save()
        self.client.force_login(user)

        with patched_urlopen(_happy_path()):
            response = self.client.get(PANEL_URL)

        self.assertIn("no-store", response.get("Cache-Control", ""))


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

@override_settings(**N8N_CONFIGURED)
class RenderingTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.superuser = make_user("root@example.de")
        self.superuser.is_staff = True
        self.superuser.is_superuser = True
        self.superuser.save()
        self.client.force_login(self.superuser)
        assert User  # silence the unused import in a readable way

    def _get(self, side_effect=None):
        with patched_urlopen(side_effect or _happy_path()):
            return self.client.get(PANEL_URL)

    def test_renders_for_a_superuser(self):
        response = self._get()

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "admin/automation/n8n_panel.html")

    def test_lists_workflow_name_id_state_and_node_count(self):
        response = self._get()
        body = response.content.decode()

        self.assertIn("FindaJob — Job Ingestion", body)
        self.assertIn("UnnxTvVi4hSEB9xJ", body)
        self.assertIn("ACTIVE", body)
        # Rankenstein has 87 nodes in the fixture; it is an "other" workflow.
        self.assertIn("Rankenstein v9 - Part 1 - Business Intelligence", body)
        self.assertIn("87", body)

    def test_findajob_workflows_are_grouped_first(self):
        response = self._get()
        body = response.content.decode()

        first_related = body.index("FindaJob — Job Ingestion")
        first_other = body.index("Rankenstein v9 - Part 1 - Business Intelligence")
        self.assertLess(first_related, first_other)

    def test_the_two_known_dead_workflows_are_marked(self):
        response = self._get()
        body = response.content.decode()

        self.assertIn("DEAD WEBHOOK", body)
        self.assertIn("no longer exist", body)
        # Both dead ids, and the reason names the vanished endpoint.
        self.assertIn("UnnxTvVi4hSEB9xJ", body)
        self.assertIn("511xLvddi0Uwwxki", body)
        self.assertIn("webhooks/generation-result/", body)

        panel = response.context["panel"]
        self.assertEqual(
            {row.workflow.id for row in panel.dead_rows},
            {"UnnxTvVi4hSEB9xJ", "511xLvddi0Uwwxki"},
        )

    def test_shows_recent_executions_with_status_and_timestamp(self):
        response = self._get()
        body = response.content.decode()

        self.assertIn("ERROR", body)
        self.assertIn("SUCCESS", body)
        self.assertIn("2026-08-01 17:00:00", body)

    def test_says_it_is_read_only(self):
        response = self._get()
        body = response.content.decode()

        self.assertIn("READ-ONLY", body)
        self.assertIn("read-only", body.lower())

    def test_only_get_requests_are_ever_made_to_n8n(self):
        methods = []

        def _capture(request, timeout=None):
            methods.append(request.get_method())
            payload = (
                WORKFLOWS_PAYLOAD if "/workflows" in request.full_url
                else EXECUTIONS_PAYLOAD
            )
            return FakeResponse(payload)

        self._get(_capture)

        self.assertTrue(methods)
        self.assertEqual(set(methods), {"GET"})

    def test_executions_are_only_fetched_for_findajob_workflows(self):
        requested = []

        def _capture(request, timeout=None):
            requested.append(request.full_url)
            payload = (
                WORKFLOWS_PAYLOAD if "/workflows" in request.full_url
                else EXECUTIONS_PAYLOAD
            )
            return FakeResponse(payload)

        self._get(_capture)

        execution_calls = [url for url in requested if "/executions" in url]
        # Three findajob workflows in the fixture, none of the other two.
        self.assertEqual(len(execution_calls), 3)
        self.assertNotIn("jfH7xTw2RGMcWt10", " ".join(execution_calls))

    @override_settings(N8N_EXECUTION_WORKFLOW_LIMIT=1)
    def test_execution_workflow_limit_is_respected(self):
        requested = []

        def _capture(request, timeout=None):
            requested.append(request.full_url)
            payload = (
                WORKFLOWS_PAYLOAD if "/workflows" in request.full_url
                else EXECUTIONS_PAYLOAD
            )
            return FakeResponse(payload)

        response = self._get(_capture)

        self.assertEqual(len([u for u in requested if "/executions" in u]), 1)
        self.assertTrue(response.context["panel"].budget_exhausted)

    def test_workflow_with_no_executions_renders(self):
        response = self._get(
            route_by_path(
                **{
                    "/workflows": WORKFLOWS_PAYLOAD,
                    "/executions": EMPTY_EXECUTIONS_PAYLOAD,
                }
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("No executions recorded.", response.content.decode())


# ---------------------------------------------------------------------------
# Degraded states — none of these may be a 500
# ---------------------------------------------------------------------------

class DegradedStateTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = make_user("ops@example.de")
        self.superuser.is_staff = True
        self.superuser.is_superuser = True
        self.superuser.save()
        self.client.force_login(self.superuser)

    @override_settings(N8N_URL="https://n8n.example.test", N8N_API_TOKEN="")
    def test_missing_token_renders_a_not_configured_panel(self):
        with patched_urlopen(AssertionError("must not be called")) as patched:
            response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 200)
        self.assertIn("not configured", response.content.decode())
        self.assertFalse(response.context["panel"].configured)
        patched.assert_not_called()

    @override_settings(N8N_URL="", N8N_API_TOKEN="")
    def test_nothing_configured_at_all_renders(self):
        response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 200)
        self.assertIn("N8N_URL", response.content.decode())

    @override_settings(**N8N_CONFIGURED)
    def test_timeout_renders_an_explanation(self):
        with patched_urlopen(socket.timeout("timed out")):
            response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Could not read from n8n", body)
        self.assertIn("timeout", body)

    @override_settings(**N8N_CONFIGURED)
    def test_http_401_renders_an_explanation(self):
        error = urllib.error.HTTPError(
            url="https://n8n.example.test/api/v1/workflows",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        with patched_urlopen(error):
            response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 200)
        self.assertIn("HTTP 401", response.content.decode())

    @override_settings(**N8N_CONFIGURED)
    def test_executions_failing_does_not_break_the_workflow_list(self):
        response_side_effect = route_by_path(
            **{
                "/workflows": WORKFLOWS_PAYLOAD,
                "/executions": urllib.error.URLError(ConnectionRefusedError(111)),
            }
        )
        with patched_urlopen(response_side_effect):
            response = self.client.get(PANEL_URL)

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("FindaJob — Job Ingestion", body)
        self.assertIn("Executions unavailable", body)


# ---------------------------------------------------------------------------
# The admin index card
# ---------------------------------------------------------------------------

@override_settings(**N8N_CONFIGURED)
class AdminIndexCardTests(PanelTestCase):
    def setUp(self):
        super().setUp()
        self.superuser = make_user("index@example.de")
        self.superuser.is_staff = True
        self.superuser.is_superuser = True
        self.superuser.save()
        self.client.force_login(self.superuser)

    def test_index_shows_the_card_and_links_to_the_panel(self):
        with patched_urlopen(AssertionError("the index must never call n8n")) as patched:
            response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("n8n workflows", body)
        self.assertIn(PANEL_URL, body)
        # Cache-only: rendering the index must never wait on a third-party host.
        patched.assert_not_called()

    def test_card_shows_counts_once_the_cache_is_warm(self):
        with patched_urlopen(_happy_path()):
            self.client.get(PANEL_URL)

        with patched_urlopen(AssertionError("the index must never call n8n")):
            response = self.client.get("/admin/")

        body = response.content.decode()
        self.assertIn("WORKFLOWS", body)
        self.assertIn("DEAD WEBHOOKS", body)

    @override_settings(N8N_URL="", N8N_API_TOKEN="")
    def test_card_degrades_when_unconfigured(self):
        response = self.client.get("/admin/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("n8n is not configured", response.content.decode())

    def test_no_template_comment_leaks_into_the_page(self):
        """Django matches ``{#.*?#}`` WITHOUT ``re.DOTALL``.

        A ``{# … #}`` comment spanning a newline is therefore not a comment at
        all: it renders as literal text on the page. This actually shipped into
        the index card once and was caught by looking at it, not by a test —
        hence this test.
        """
        with patched_urlopen(_happy_path()):
            self.client.get(PANEL_URL)
        pages = ["/admin/", PANEL_URL]

        with patched_urlopen(_happy_path()):
            for url in pages:
                with self.subTest(url=url):
                    body = self.client.get(url).content.decode()
                    self.assertNotIn("{#", body)
                    self.assertNotIn("{%", body)
                    self.assertNotIn("{{", body)
