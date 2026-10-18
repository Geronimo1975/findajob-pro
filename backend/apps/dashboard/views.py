"""Server-rendered dashboard served at ``/app/``."""

from django.shortcuts import render


def index(request):
    return render(request, "dashboard_app/index.html", {})
