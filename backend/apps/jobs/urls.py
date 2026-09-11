"""Jobs URLs — mounted at the API root (spec ENDPOINTS: /platforms/, /jobs/, /documents/)."""

from django.urls import path

from apps.jobs.views import DocumentListCreateView, JobListView, PlatformListView

app_name = "jobs"

urlpatterns = [
    path("platforms/", PlatformListView.as_view(), name="platform-list"),
    path("jobs/", JobListView.as_view(), name="job-list"),
    path("documents/", DocumentListCreateView.as_view(), name="document-list"),
]
