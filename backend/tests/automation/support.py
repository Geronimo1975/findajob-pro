"""Fixtures shared by the automation tests.

The sample payloads are trimmed copies of what ``app.n8n-studio.com`` actually
returned on 2026-08-01 — same field names, same ISO-8601 ``Z`` timestamps, same
two dead findajob workflows, same "3 of the last 5 ingestion runs errored"
reality. Tests assert against the real shape, not an invented one.
"""

from __future__ import annotations

import contextlib
import json
from unittest import mock

from apps.automation import client

WORKFLOW_INGESTION_ID = "UnnxTvVi4hSEB9xJ"
WORKFLOW_GENERATION_ID = "511xLvddi0Uwwxki"


def _workflow(wid, name, active, nodes, updated, *, archived=False, triggers=1):
    return {
        "id": wid,
        "name": name,
        "active": active,
        "isArchived": archived,
        "triggerCount": triggers,
        "createdAt": "2026-07-01T09:00:00.000Z",
        "updatedAt": updated,
        "nodes": [{"type": "n8n-nodes-base.noOp", "name": f"n{i}"} for i in range(nodes)],
        "tags": [],
    }


WORKFLOWS_PAYLOAD = {
    "data": [
        _workflow(
            WORKFLOW_INGESTION_ID,
            "FindaJob — Job Ingestion",
            True,
            4,
            "2026-07-21T20:52:04.161Z",
        ),
        _workflow(
            WORKFLOW_GENERATION_ID,
            "FindaJob — Application Generation",
            True,
            7,
            "2026-07-22T11:13:26.198Z",
        ),
        _workflow("cINDCLxxAeFi2UrB", "JobApp Trigger", True, 2, "2026-07-23T05:05:03.679Z"),
        _workflow(
            "jfH7xTw2RGMcWt10",
            "Rankenstein v9 - Part 1 - Business Intelligence",
            True,
            87,
            "2025-12-27T10:10:56.220Z",
        ),
        _workflow("qAz7SQfc314xXY9G", "NC Email Poller", False, 3, "2025-12-27T10:10:56.220Z"),
    ],
    "nextCursor": None,
}

EXECUTIONS_PAYLOAD = {
    "data": [
        {
            "id": 3622,
            "workflowId": WORKFLOW_INGESTION_ID,
            "status": "error",
            "mode": "trigger",
            "startedAt": "2026-08-01T17:00:00.073Z",
            "stoppedAt": "2026-08-01T17:00:01.236Z",
            "finished": False,
        },
        {
            "id": 3608,
            "workflowId": WORKFLOW_INGESTION_ID,
            "status": "success",
            "mode": "trigger",
            "startedAt": "2026-07-31T05:00:00.040Z",
            "stoppedAt": "2026-07-31T05:00:01.018Z",
            "finished": True,
        },
    ],
    "nextCursor": None,
}

EMPTY_EXECUTIONS_PAYLOAD = {"data": [], "nextCursor": None}


class FakeResponse:
    """The bare minimum of what ``urlopen`` returns, as a context manager."""

    def __init__(self, payload):
        self._body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self, amount=None):
        return self._body if amount is None else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


@contextlib.contextmanager
def patched_urlopen(side_effect):
    """Patch the client's HTTP transport and hand back the mock.

    ``side_effect`` may be a callable, an exception instance/class, or a list.

    Patches ``_NO_REDIRECT_OPENER.open`` rather than ``urllib.request.urlopen``:
    the client deliberately does not use the module-level ``urlopen``, because
    the global opener installs ``HTTPRedirectHandler``, which copies headers —
    including ``X-N8N-API-KEY`` — onto a cross-host redirect target. Patching
    the symbol the client no longer calls would make every one of these tests
    pass against code that never ran.
    """
    with mock.patch.object(
        client._NO_REDIRECT_OPENER, "open", side_effect=side_effect
    ) as patched:
        yield patched


def route_by_path(**payloads):
    """Return a urlopen side effect that dispatches on the request path."""

    def _side_effect(request, timeout=None):
        url = request.full_url
        for fragment, payload in payloads.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return FakeResponse(payload)
        raise AssertionError(f"Unexpected n8n request: {url}")

    return _side_effect
