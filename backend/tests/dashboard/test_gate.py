"""Dashboard gate: anonymous visitors are sent to the static login page."""

import pytest
from django.test import Client

from tests.support import make_user


def test_anonymous_redirected_to_auth(client):
    response = client.get("/app/")
    assert response.status_code == 302
    assert response["Location"].startswith("/auth.html?next=%2Fapp%2F")


@pytest.mark.django_db
def test_authenticated_gets_200():
    client = Client()
    client.force_login(make_user("gate@example.com"))
    response = client.get("/app/")
    assert response.status_code == 200


def test_unknown_app_path_404(client):
    assert client.get("/app/nope/").status_code == 404
