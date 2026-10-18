"""Server-rendered dashboard served at ``/app/``."""

from django.shortcuts import render

from .auth import session_required
from .kpis import build_command_center
from .orgs import resolve_org


@session_required
def index(request):
    return render(
        request,
        "dashboard_app/index.html",
        {"cc": build_command_center(resolve_org(request), request.user)},
    )
