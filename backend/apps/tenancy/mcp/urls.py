"""URLConf for ``/mcp/`` (mounted from ``config/urls.py``)."""
from __future__ import annotations

from django.urls import path

from apps.tenancy.mcp.views import McpEndpoint

app_name = "mcp"

urlpatterns = [path("", McpEndpoint.as_view(), name="endpoint")]
