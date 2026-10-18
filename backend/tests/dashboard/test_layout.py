"""Jobick shell: sidebar chrome, command-center tiles, local-only assets."""

import re

import pytest
from django.core.management import call_command
from django.test import Client

from apps.jobs.models import Application, ApplicationStatus as S
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_models import make_listing
from tests.support import make_user

pytestmark = pytest.mark.django_db

NAV = ["Dashboard", "Jobs", "Applications", "Profile &amp; CV", "Documents",
       "Recommendations", "Assistant", "Settings"]


def _login():
    call_command("seed_platforms", verbosity=0)
    user = make_user("shell@example.com")
    org = bootstrap_workspace(user).org
    client = Client()
    client.force_login(user)
    return client, user, org


def test_index_renders_shell():
    client, user, org = _login()
    for n, status in enumerate([S.SENT, S.SENT, S.INTERVIEW, S.RUNNING]):
        Application.objects.create(
            org=org, user=user, status=status,
            job_listing=make_listing(url=f"https://remoteok.com/jobs/{n}"),
        )
    html = client.get("/app/").content.decode()
    for label in NAV:
        assert label in html
    for label in ("Applications Sent", "In Progress", "Interviews"):
        assert label in html
    assert re.search(r'data-kpi="sent">2<', html)
    assert re.search(r'data-kpi="in_progress">1<', html)
    assert re.search(r'data-kpi="interviews">1<', html)
    assert "/static/dashboard_app/css/style.css" in html
    assert "/static/dashboard_app/vendor/global/global.min.js" in html
    assert "Franklin" not in html


def test_empty_states_and_no_external_origin():
    client, *_ = _login()
    html = client.get("/app/").content.decode()
    assert "No jobs yet" in html
    assert "No active applications" in html
    assert not re.findall(r'(?:src|href)="(?:https?:)?//', html)
