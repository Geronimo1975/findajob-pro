"""resolve_org: server-rendered org pick. Foreign ?org= falls back, never 403s."""

import pytest
from django.test import RequestFactory

from apps.dashboard.orgs import resolve_org
from apps.tenancy.models import Membership, Organization
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user

pytestmark = pytest.mark.django_db


def _request(user, path="/app/", **extra):
    request = RequestFactory().get(path, **extra)
    request.user = user
    return request


def _second_org(user, slug):
    org = Organization.objects.create(name=slug, slug=slug)
    Membership.objects.create(org=org, user=user)
    return org


def test_oldest_membership_default():
    user = make_user("a@example.com")
    first = bootstrap_workspace(user).org
    _second_org(user, "second-a")
    assert resolve_org(_request(user)) == first


def test_org_query_param_honored_when_member():
    user = make_user("b@example.com")
    bootstrap_workspace(user)
    second = _second_org(user, "second-b")
    assert resolve_org(_request(user, f"/app/?org={second.pk}")) == second
    header = _request(user, HTTP_X_FINDAJOB_ORG=str(second.pk))
    assert resolve_org(header) == second


def test_foreign_org_param_falls_back():
    user = make_user("c@example.com")
    own = bootstrap_workspace(user).org
    foreign = bootstrap_workspace(make_user("d@example.com")).org
    assert resolve_org(_request(user, f"/app/?org={foreign.pk}")) == own
    assert resolve_org(_request(user, "/app/?org=garbage")) == own


def test_no_membership_returns_none():
    user = make_user("e@example.com")
    assert resolve_org(_request(user)) is None
