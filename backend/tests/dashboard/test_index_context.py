"""build_command_center: KPIs and feeds on real, org/user-scoped data."""

import pytest
from django.core.management import call_command

from apps.dashboard.kpis import build_command_center
from apps.jobs.models import Application, ApplicationStatus as S
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_models import make_listing
from tests.support import make_user

pytestmark = pytest.mark.django_db


def _workspace():
    call_command("seed_platforms", verbosity=0)
    user = make_user()
    return user, bootstrap_workspace(user).org


def _apply(user, org, status, n):
    listing = make_listing(url=f"https://remoteok.com/jobs/{n}")
    return Application.objects.create(
        org=org, user=user, job_listing=listing, status=status
    )


def test_kpis_count_by_status():
    user, org = _workspace()
    statuses = [S.SENT, S.SENT, S.PENDING, S.RUNNING, S.REVIEW, S.SENDING,
                S.INTERVIEW, S.FAILED]
    for n, status in enumerate(statuses):
        _apply(user, org, status, n)
    cc = build_command_center(org, user)
    assert cc["kpis"] == {"sent": 2, "in_progress": 4, "interviews": 1}
    assert len(cc["recent_jobs"]) == len(statuses)


def test_empty_workspace_zeroes():
    cc = build_command_center(None, make_user())
    assert cc == {
        "kpis": {"sent": 0, "in_progress": 0, "interviews": 0},
        "recent_jobs": [],
        "active_applications": [],
        "documents_summary": [],
        "job_rows": [],
        "recommendations": [],
        "ingestion_runs": [],
        "min_prescore": None,
    }
    user, org = _workspace()
    cc = build_command_center(org, user)
    assert cc["kpis"] == {"sent": 0, "in_progress": 0, "interviews": 0}
    assert cc["active_applications"] == []


def test_active_excludes_terminal():
    user, org = _workspace()
    keep = _apply(user, org, S.SENT, 1)
    _apply(user, org, S.REJECTED, 2)
    _apply(user, org, S.OFFER, 3)
    cc = build_command_center(org, user)
    assert [a.pk for a in cc["active_applications"]] == [keep.pk]
