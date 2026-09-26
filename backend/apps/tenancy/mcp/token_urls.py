from django.urls import path

from apps.tenancy.mcp.token_views import McpTokenDetailView, McpTokenListView

app_name = "mcp_tokens"

urlpatterns = [
    path("", McpTokenListView.as_view(), name="list"),
    path("<int:token_id>/", McpTokenDetailView.as_view(), name="detail"),
]
