"""Dashboard shell: the server-rendered app mounted at /app/."""

from django.urls import reverse


def test_app_root_url_resolves():
    assert reverse("dashboard:index") == "/app/"
