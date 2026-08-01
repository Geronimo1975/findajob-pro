"""Template tag behind the admin-index card.

The card is included from ``admin/index.html``, whose context belongs to Django's
admin and cannot be extended without owning that view. A ``simple_tag`` makes
``admin/_n8n_panel.html`` self-sufficient: it can be dropped into any template by
any agent, with no context-processor and no view change.

The tag is **cache-only** — see :func:`apps.automation.panel.build_summary`. It
must never make the admin index wait on a third-party host.
"""

from __future__ import annotations

from django import template

from apps.automation.client import get_client
from apps.automation.panel import PanelSummary, build_summary

register = template.Library()


@register.simple_tag
def n8n_panel_summary() -> PanelSummary:
    """Counts from the 60 s response cache, or a "not warm yet" summary."""
    try:
        return build_summary(get_client())
    except Exception:  # pragma: no cover - the card must never break the index
        return PanelSummary(configured=False)
