"""Platform adapters — one isolated module per source (architecture rule 2).

New platforms are added by filling the :class:`~apps.jobs.adapters.base.BaseAdapter`
contract and registering the class here, never by writing bespoke logic into
the core. Only LOW-risk feed/API platforms get adapters at all: LinkedIn,
Indeed and Wellfound are semi-manual by law of the risk table and have no
entry in this registry on purpose.
"""

from __future__ import annotations

from apps.jobs.adapters.base import BaseAdapter, NormalizedJob
from apps.jobs.adapters.remoteok import RemoteOKAdapter
from apps.jobs.adapters.wwr import WWRAdapter

ADAPTERS: dict[str, type[BaseAdapter]] = {
    RemoteOKAdapter.slug: RemoteOKAdapter,
    WWRAdapter.slug: WWRAdapter,
}

__all__ = ["ADAPTERS", "BaseAdapter", "NormalizedJob"]
