"""Settings accessors with safe defaults.

Read through functions, not module constants, so ``@override_settings`` in a
test (and a settings reload in a shell) takes effect without a reimport.
"""

from __future__ import annotations

from django.conf import settings


def _int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def workflow_limit() -> int:
    """How many workflows to ask n8n for. The instance has 39."""
    return _int("N8N_WORKFLOW_LIMIT", 100, minimum=1, maximum=250)


def execution_limit() -> int:
    """Recent executions shown per workflow."""
    return _int("N8N_EXECUTION_LIMIT", 5, minimum=1, maximum=25)


def execution_workflow_limit() -> int:
    """How many workflows get an execution lookup at all.

    One HTTP round trip each, so this is the real cost driver. Only findajob's
    own workflows are ever candidates.
    """
    return _int("N8N_EXECUTION_WORKFLOW_LIMIT", 8, minimum=0, maximum=40)


def panel_budget_seconds() -> float:
    """Wall-clock ceiling for the whole execution-fetch loop."""
    return float(_int("N8N_PANEL_BUDGET", 12, minimum=1, maximum=60))
