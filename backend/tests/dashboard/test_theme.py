"""Voltagent dark theme is the default; a topbar toggle flips to light."""

import re

import pytest
from django.core.management import call_command
from django.test import Client

from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user

pytestmark = pytest.mark.django_db


def _client():
    call_command("seed_platforms", verbosity=0)
    user = make_user("theme@example.com")
    bootstrap_workspace(user)
    client = Client()
    client.force_login(user)
    return client


def test_default_theme_is_dark():
    resp = _client().get("/app/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert re.search(r"<html[^>]*data-theme=\"dark\"", html)
    assert 'id="theme-toggle"' in html
    assert "dashboard_app/css/theme-voltagent.css" in html
    # no-flash script must run in <head>, before the body paints
    head = html.split("</head>")[0]
    assert "fj-theme" in head
