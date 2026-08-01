"""Turning raw n8n data into the shape the admin panel renders.

Kept separate from the view so the ordering, grouping and dead-webhook rules are
plain functions with no request, no template and no network.

Two product facts are encoded here:

1. **Which workflows are ours.** The instance is a personal n8n account shared
   with unrelated work (SEO pipelines, Telegram bots, Rotocon CRM). Of its 39
   workflows, 7 belong to findajob: the two ``FindaJob — …`` ones and five
   ``JobApp …`` ones. They are matched by name and shown first.

2. **Which of ours are dead.** ``FindaJob — Job Ingestion`` and
   ``FindaJob — Application Generation`` still call ``https://findajob.pro/webhooks/…``
   endpoints that were removed when the old platform was wiped on 2026-08-01.
   Ingestion is on a 07:00/19:00 UTC schedule and has been erroring on every
   tick since. Surfacing that is a large part of why this panel exists.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

from apps.automation.client import (
    ErrorKind,
    N8nClient,
    N8nExecution,
    N8nResult,
    N8nWorkflow,
)

#: Case-insensitive substrings that mark a workflow as findajob's.
FINDAJOB_NAME_MARKERS: tuple[str, ...] = ("findajob", "jobapp", "job app", "job-app")

#: Workflows known to point at endpoints that no longer exist. Keyed by n8n
#: workflow id, which is stable across renames.
DEAD_WEBHOOK_WORKFLOWS: dict[str, str] = {
    "UnnxTvVi4hSEB9xJ": (
        "Calls https://findajob.pro/webhooks/search-profiles/ and "
        "https://findajob.pro/webhooks/jobs/ — both endpoints belonged to the old "
        "platform and were removed on 2026-08-01. It is still active on a "
        "07:00 / 19:00 UTC schedule, so every tick fails. Deactivate it in n8n or "
        "repoint it once the new ingestion API exists."
    ),
    "511xLvddi0Uwwxki": (
        "Posts its result back to https://findajob.pro/webhooks/generation-result/, "
        "which no longer exists, and its inbound webhook "
        "(POST /webhook/findajob-generate) was only ever called by a Django view "
        "that was wiped on 2026-08-01. Nothing triggers it and nothing receives it."
    ),
}


def is_findajob_workflow(workflow: N8nWorkflow) -> bool:
    """True when the workflow's name marks it as belonging to this product."""
    name = workflow.name.casefold()
    return any(marker in name for marker in FINDAJOB_NAME_MARKERS)


@dataclass(frozen=True, slots=True)
class WorkflowRow:
    """One rendered row: the workflow plus whatever we learned about its runs."""

    workflow: N8nWorkflow
    is_related: bool
    dead_reason: str = ""
    external_url: str = ""
    executions: tuple[N8nExecution, ...] = ()
    executions_error: str = ""
    executions_fetched: bool = False

    @property
    def is_dead(self) -> bool:
        return bool(self.dead_reason)

    @property
    def last_execution(self) -> N8nExecution | None:
        return self.executions[0] if self.executions else None

    @property
    def recent_failures(self) -> int:
        return sum(1 for execution in self.executions if execution.is_failure)


@dataclass(frozen=True, slots=True)
class PanelData:
    """Everything the template needs. Always renderable — never an exception."""

    configured: bool
    base_url: str = ""
    error: str = ""
    error_kind: str = ErrorKind.NONE
    status_code: int | None = None
    related: tuple[WorkflowRow, ...] = ()
    others: tuple[WorkflowRow, ...] = ()
    fetched_at: datetime | None = None
    from_cache: bool = False
    budget_exhausted: bool = False
    execution_limit: int = 0

    @property
    def ok(self) -> bool:
        return self.configured and not self.error

    @property
    def all_rows(self) -> tuple[WorkflowRow, ...]:
        return self.related + self.others

    @property
    def total(self) -> int:
        return len(self.all_rows)

    @property
    def active_total(self) -> int:
        return sum(1 for row in self.all_rows if row.workflow.active)

    @property
    def dead_rows(self) -> tuple[WorkflowRow, ...]:
        return tuple(row for row in self.related if row.is_dead)

    @property
    def failing_rows(self) -> tuple[WorkflowRow, ...]:
        """Related workflows whose most recent run failed."""
        return tuple(
            row
            for row in self.related
            if row.last_execution is not None and row.last_execution.is_failure
        )


def _sort_key(row: WorkflowRow):
    """Dead first, then active, then most-recently-updated, then name.

    ``updated_at`` may be ``None``; sorting on a naive/None mix would raise, so
    the timestamp is folded to a float.
    """
    updated = row.workflow.updated_at.timestamp() if row.workflow.updated_at else 0.0
    return (not row.is_dead, not row.workflow.active, -updated, row.workflow.name.casefold())


def build_panel(
    client: N8nClient,
    *,
    workflow_limit: int = 100,
    execution_limit: int = 5,
    execution_workflow_limit: int = 8,
    budget_seconds: float = 12.0,
) -> PanelData:
    """Fetch and arrange everything the panel shows.

    Executions are fetched **only for related workflows**, at most
    ``execution_workflow_limit`` of them, and the whole loop is bounded by
    ``budget_seconds`` of wall clock. One request per workflow at a 5 s timeout
    would otherwise let a slow n8n hold the admin open for minutes; the 60 s
    response cache means the budget is normally paid once a minute at most.
    """
    if not client.is_configured:
        result = client.workflows(limit=1)
        return PanelData(
            configured=False,
            error=result.error or "n8n is not configured.",
            error_kind=ErrorKind.NOT_CONFIGURED,
            execution_limit=execution_limit,
        )

    result: N8nResult = client.workflows(limit=workflow_limit)
    if not result.ok:
        return PanelData(
            configured=True,
            base_url=client.base_url,
            error=result.error,
            error_kind=result.error_kind,
            status_code=result.status_code,
            from_cache=result.from_cache,
            execution_limit=execution_limit,
        )

    related: list[WorkflowRow] = []
    others: list[WorkflowRow] = []
    for workflow in result.workflows:
        row = WorkflowRow(
            workflow=workflow,
            is_related=is_findajob_workflow(workflow),
            dead_reason=DEAD_WEBHOOK_WORKFLOWS.get(workflow.id, ""),
            external_url=client.workflow_url(workflow.id),
        )
        (related if row.is_related else others).append(row)

    related.sort(key=_sort_key)
    others.sort(key=_sort_key)

    deadline = time.monotonic() + budget_seconds
    budget_exhausted = False
    enriched: list[WorkflowRow] = []
    for index, row in enumerate(related):
        if index >= execution_workflow_limit or time.monotonic() >= deadline:
            budget_exhausted = True
            enriched.append(row)
            continue
        runs = client.executions(row.workflow.id, limit=execution_limit)
        enriched.append(
            WorkflowRow(
                workflow=row.workflow,
                is_related=row.is_related,
                dead_reason=row.dead_reason,
                external_url=row.external_url,
                executions=runs.executions,
                executions_error="" if runs.ok else runs.error,
                executions_fetched=runs.ok,
            )
        )

    return PanelData(
        configured=True,
        base_url=client.base_url,
        related=tuple(enriched),
        others=tuple(others),
        fetched_at=result.fetched_at,
        from_cache=result.from_cache,
        budget_exhausted=budget_exhausted,
        execution_limit=execution_limit,
    )


@dataclass(frozen=True, slots=True)
class PanelSummary:
    """The tiny, never-blocking summary the admin index card renders."""

    configured: bool
    warm: bool = False
    total: int = 0
    active: int = 0
    related: int = 0
    dead: int = 0
    base_url: str = ""


def build_summary(client: N8nClient) -> PanelSummary:
    """Cache-only counts for the admin index.

    Deliberately never reaches the network: the admin index is the first page
    every staff user loads and it must not inherit n8n's latency or downtime.
    """
    if not client.is_configured:
        return PanelSummary(configured=False)

    result = client.workflows(cached_only=True)
    if not result.ok:
        return PanelSummary(configured=True, warm=False, base_url=client.base_url)

    related = [w for w in result.workflows if is_findajob_workflow(w)]
    return PanelSummary(
        configured=True,
        warm=True,
        total=len(result.workflows),
        active=sum(1 for w in result.workflows if w.active),
        related=len(related),
        dead=sum(1 for w in related if w.id in DEAD_WEBHOOK_WORKFLOWS),
        base_url=client.base_url,
    )
