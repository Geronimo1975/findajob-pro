"""Server-rendered dashboard served at ``/app/``."""

from django.shortcuts import render

from .auth import session_required


@session_required
def index(request):
    return render(request, "dashboard_app/index.html", {})
