from __future__ import annotations

from django.urls import path

from apps.leads.views import DemoLeadView

app_name = "leads"
urlpatterns = [path("demo/", DemoLeadView.as_view(), name="demo")]
