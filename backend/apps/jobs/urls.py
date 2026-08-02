"""Jobs URLs — mounted at the API root (spec ENDPOINTS: /platforms/, /jobs/)."""

from django.urls import path

from apps.jobs.views import JobListView, PlatformListView

app_name = "jobs"

urlpatterns = [
    path("platforms/", PlatformListView.as_view(), name="platform-list"),
    path("jobs/", JobListView.as_view(), name="job-list"),
]
