"""Jobs URLs — mounted at the API root (spec ENDPOINTS: /platforms/, /jobs/, /documents/)."""

from django.urls import path

from apps.jobs.views import (
    DocumentDetailView, DocumentDownloadView, DocumentListCreateView, JobListView,
    PlatformListView,
)

app_name = "jobs"

urlpatterns = [
    path("platforms/", PlatformListView.as_view(), name="platform-list"),
    path("jobs/", JobListView.as_view(), name="job-list"),
    path("documents/", DocumentListCreateView.as_view(), name="document-list"),
    path("documents/<int:pk>/", DocumentDetailView.as_view(), name="document-detail"),
    path(
        "documents/<int:pk>/download/",
        DocumentDownloadView.as_view(),
        name="document-download",
    ),
]
