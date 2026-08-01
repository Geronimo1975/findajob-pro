"""The client must never raise into a view.

Every failure mode below has exactly one acceptable outcome: a returned
:class:`N8nResult` with ``ok=False`` and a specific ``error_kind``. If any of
these ever propagates an exception instead, ``/admin/automation/`` becomes a 500
and the panel stops being safe to link from the admin index.
"""

from __future__ import annotations

import socket
import urllib.error
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from apps.automation.client import (
    ErrorKind,
    N8nClient,
    N8nExecution,
    get_client,
    parse_timestamp,
)
from tests.automation.support import (
    EXECUTIONS_PAYLOAD,
    WORKFLOWS_PAYLOAD,
    WORKFLOW_INGESTION_ID,
    FakeResponse,
    patched_urlopen,
)


def _client(**overrides) -> N8nClient:
    defaults = dict(
        base_url="https://n8n.example.test",
        token="test-token",
        timeout=5.0,
        cache_ttl=60,
        error_cache_ttl=15,
    )
    defaults.update(overrides)
    return N8nClient(**defaults)


class ClientCacheIsolation(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

class MissingConfigurationTests(ClientCacheIsolation):
    def test_missing_token_is_a_typed_result_not_an_exception(self):
        result = _client(token="").workflows()

        self.assertFalse(result.ok)
        self.assertTrue(result.not_configured)
        self.assertEqual(result.error_kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("N8N_API_TOKEN", result.error)

    def test_missing_url_is_a_typed_result(self):
        result = _client(base_url="").workflows()

        self.assertEqual(result.error_kind, ErrorKind.NOT_CONFIGURED)
        self.assertIn("N8N_URL", result.error)

    def test_non_http_scheme_is_refused(self):
        """A file:// or javascript: N8N_URL must never be dialled or rendered."""
        result = _client(base_url="file:///etc/passwd").workflows()

        self.assertEqual(result.error_kind, ErrorKind.NOT_CONFIGURED)
        self.assertEqual(_client(base_url="file:///etc/passwd").workflow_url("x"), "")

    def test_unconfigured_client_makes_no_request(self):
        with patched_urlopen(AssertionError("must not be called")) as patched:
            _client(token="").workflows()
        patched.assert_not_called()

    @override_settings(
        N8N_URL="https://from-settings.test",
        N8N_API_TOKEN="tok",
        N8N_TIMEOUT=7,
        N8N_CACHE_TTL=90,
    )
    def test_get_client_reads_settings(self):
        client = get_client()

        self.assertEqual(client.base_url, "https://from-settings.test")
        self.assertEqual(client.timeout, 7.0)
        self.assertEqual(client.cache_ttl, 90)
        self.assertTrue(client.is_configured)

    def test_token_is_not_in_the_client_repr(self):
        self.assertNotIn("test-token", repr(_client()))


# ---------------------------------------------------------------------------
# Transport failures
# ---------------------------------------------------------------------------

class TransportFailureTests(ClientCacheIsolation):
    def test_socket_timeout_returns_timeout_kind(self):
        with patched_urlopen(socket.timeout("timed out")):
            result = _client().workflows()

        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.TIMEOUT)
        self.assertIn("5s", result.error)

    def test_urlerror_wrapping_a_timeout_is_still_a_timeout(self):
        with patched_urlopen(urllib.error.URLError(socket.timeout("timed out"))):
            result = _client().workflows()

        self.assertEqual(result.error_kind, ErrorKind.TIMEOUT)

    def test_http_error_returns_http_kind_and_status(self):
        error = urllib.error.HTTPError(
            url="https://n8n.example.test/api/v1/workflows",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )
        with patched_urlopen(error):
            result = _client().workflows()

        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.HTTP)
        self.assertEqual(result.status_code, 401)

    def test_connection_refused_returns_network_kind(self):
        with patched_urlopen(urllib.error.URLError(ConnectionRefusedError(111))):
            result = _client().workflows()

        self.assertEqual(result.error_kind, ErrorKind.NETWORK)

    def test_malformed_json_returns_decode_kind(self):
        with patched_urlopen(lambda request, timeout=None: FakeResponse(b"<html>502</html>")):
            result = _client().workflows()

        self.assertEqual(result.error_kind, ErrorKind.DECODE)

    def test_json_body_without_a_data_list_returns_decode_kind(self):
        with patched_urlopen(lambda request, timeout=None: FakeResponse({"message": "nope"})):
            result = _client().workflows()

        self.assertEqual(result.error_kind, ErrorKind.DECODE)

    def test_unexpected_exception_is_swallowed(self):
        """Defence in depth: even a bug in urllib must not reach the view."""
        with patched_urlopen(RuntimeError("boom")):
            result = _client().workflows()

        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.UNEXPECTED)
        self.assertNotIn("boom", result.error)

    def test_failures_are_negatively_cached(self):
        """A dead n8n must cost one timeout, not one per page load."""
        with patched_urlopen(socket.timeout("timed out")) as patched:
            first = _client().workflows()
            second = _client().workflows()

        self.assertEqual(patched.call_count, 1)
        self.assertEqual(first.error_kind, ErrorKind.TIMEOUT)
        self.assertEqual(second.error_kind, ErrorKind.TIMEOUT)
        self.assertTrue(second.from_cache)


# ---------------------------------------------------------------------------
# Success paths
# ---------------------------------------------------------------------------

class SuccessTests(ClientCacheIsolation):
    def test_workflows_are_parsed_and_projected(self):
        with patched_urlopen(lambda request, timeout=None: FakeResponse(WORKFLOWS_PAYLOAD)):
            result = _client().workflows()

        self.assertTrue(result.ok)
        self.assertEqual(len(result.workflows), 5)

        ingestion = next(w for w in result.workflows if w.id == WORKFLOW_INGESTION_ID)
        self.assertEqual(ingestion.name, "FindaJob — Job Ingestion")
        self.assertTrue(ingestion.active)
        self.assertEqual(ingestion.node_count, 4)
        self.assertEqual(ingestion.updated_at.year, 2026)
        self.assertIsNotNone(ingestion.updated_at.tzinfo)

    def test_request_is_a_get_with_the_api_key_header(self):
        captured = {}

        def _capture(request, timeout=None):
            captured["method"] = request.get_method()
            captured["headers"] = dict(request.header_items())
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeResponse(WORKFLOWS_PAYLOAD)

        with patched_urlopen(_capture):
            _client().workflows()

        self.assertEqual(captured["method"], "GET")
        # urllib title-cases header names.
        self.assertEqual(captured["headers"]["X-n8n-api-key"], "test-token")
        self.assertIn("/api/v1/workflows", captured["url"])
        self.assertEqual(captured["timeout"], 5.0)

    def test_second_call_is_served_from_cache(self):
        with patched_urlopen(
            lambda request, timeout=None: FakeResponse(WORKFLOWS_PAYLOAD)
        ) as patched:
            first = _client().workflows()
            second = _client().workflows()

        self.assertEqual(patched.call_count, 1)
        self.assertFalse(first.from_cache)
        self.assertTrue(second.from_cache)
        self.assertEqual(
            [w.id for w in first.workflows], [w.id for w in second.workflows]
        )
        self.assertEqual(first.workflows[0].updated_at, second.workflows[0].updated_at)

    def test_a_different_token_does_not_reuse_the_cache(self):
        with patched_urlopen(
            lambda request, timeout=None: FakeResponse(WORKFLOWS_PAYLOAD)
        ) as patched:
            _client().workflows()
            _client(token="rotated").workflows()

        self.assertEqual(patched.call_count, 2)

    def test_cache_key_never_contains_the_token(self):
        client = _client()
        key = client._cache_key("/workflows", {"limit": 100})

        self.assertNotIn("test-token", key)
        self.assertTrue(key.startswith("findajob:n8n:"))

    def test_executions_are_parsed(self):
        with patched_urlopen(lambda request, timeout=None: FakeResponse(EXECUTIONS_PAYLOAD)):
            result = _client().executions(WORKFLOW_INGESTION_ID)

        self.assertTrue(result.ok)
        self.assertEqual(len(result.executions), 2)
        self.assertTrue(result.executions[0].is_failure)
        self.assertTrue(result.executions[1].is_success)
        self.assertEqual(result.executions[0].workflow_id, WORKFLOW_INGESTION_ID)

    def test_executions_query_carries_the_workflow_id_and_limit(self):
        captured = {}

        def _capture(request, timeout=None):
            captured["url"] = request.full_url
            return FakeResponse(EXECUTIONS_PAYLOAD)

        with patched_urlopen(_capture):
            _client().executions(WORKFLOW_INGESTION_ID, limit=3)

        self.assertIn("/api/v1/executions", captured["url"])
        self.assertIn(f"workflowId={WORKFLOW_INGESTION_ID}", captured["url"])
        self.assertIn("limit=3", captured["url"])

    def test_cached_only_never_touches_the_network(self):
        with patched_urlopen(AssertionError("must not be called")) as patched:
            result = _client().workflows(cached_only=True)

        patched.assert_not_called()
        self.assertFalse(result.ok)
        self.assertEqual(result.error_kind, ErrorKind.CACHE_MISS)

    def test_cached_only_serves_a_warm_cache(self):
        with patched_urlopen(lambda request, timeout=None: FakeResponse(WORKFLOWS_PAYLOAD)):
            _client().workflows()

        with patched_urlopen(AssertionError("must not be called")):
            result = _client().workflows(cached_only=True)

        self.assertTrue(result.ok)
        self.assertEqual(len(result.workflows), 5)

    def test_workflow_url_is_built_from_the_validated_base(self):
        self.assertEqual(
            _client().workflow_url("abc/../def"),
            "https://n8n.example.test/workflow/abc%2F..%2Fdef",
        )


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------

class BrokenCacheTests(ClientCacheIsolation):
    def test_a_broken_cache_backend_degrades_to_uncached(self):
        """Redis is cache-only here; an outage must not take the panel down."""
        with mock.patch(
            "apps.automation.client.cache.get", side_effect=RuntimeError("redis down")
        ), mock.patch(
            "apps.automation.client.cache.set", side_effect=RuntimeError("redis down")
        ), patched_urlopen(
            lambda request, timeout=None: FakeResponse(WORKFLOWS_PAYLOAD)
        ):
            result = _client().workflows()

        self.assertTrue(result.ok)
        self.assertEqual(len(result.workflows), 5)


class TimestampParsingTests(SimpleTestCase):
    def test_z_suffix_becomes_utc_aware(self):
        parsed = parse_timestamp("2026-08-01T17:00:00.073Z")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.utcoffset().total_seconds(), 0)

    def test_naive_value_is_made_aware(self):
        parsed = parse_timestamp("2026-08-01T17:00:00")

        self.assertIsNotNone(parsed.tzinfo)

    def test_garbage_is_none_not_an_exception(self):
        self.assertIsNone(parse_timestamp("not a date"))
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp(12345))


class ExecutionStatusTests(SimpleTestCase):
    def test_failure_statuses(self):
        for status in ("error", "crashed", "canceled"):
            with self.subTest(status=status):
                execution = N8nExecution.from_api({"id": 1, "status": status})
                self.assertTrue(execution.is_failure)
                self.assertFalse(execution.is_success)

    def test_success_status(self):
        execution = N8nExecution.from_api({"id": 1, "status": "success"})

        self.assertTrue(execution.is_success)
        self.assertFalse(execution.is_failure)

    def test_running_is_neither(self):
        execution = N8nExecution.from_api({"id": 1, "status": "running"})

        self.assertFalse(execution.is_success)
        self.assertFalse(execution.is_failure)
