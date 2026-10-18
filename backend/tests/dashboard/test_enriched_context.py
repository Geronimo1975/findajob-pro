"""build_command_center enrichment: documents, match-scored jobs, ingestion runs."""

from datetime import timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.utils import timezone

from apps.dashboard.kpis import build_command_center
from apps.jobs.models import Document, DocumentKind
from apps.profiles.services import get_or_create_search_profile
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_models import make_listing
from tests.support import make_user

pytestmark = pytest.mark.django_db


def _workspace():
    call_command("seed_platforms", verbosity=0)
    user = make_user()
    return user, bootstrap_workspace(user).org


def _doc(org, user, kind, n=0):
    return Document.objects.create(
        org=org, user=user, name=f"d{n}", kind=kind,
        file=ContentFile(b"x", name=f"d{n}.pdf"),
    )


def _profile(org, user, **kw):
    p = get_or_create_search_profile(org, user)
    for k, v in kw.items():
        setattr(p, k, v)
    p.save()
    return p


def test_job_rows_have_scores_sorted_desc():
    user, org = _workspace()
    _profile(org, user, skills_primary=["django", "drf"], min_prescore=50)
    make_listing(url="https://remoteok.com/jobs/1", title="Cook", tags=[])
    make_listing(url="https://remoteok.com/jobs/2", tags=["Django", "DRF"])
    make_listing(url="https://remoteok.com/jobs/3", title="Django dev", tags=[])
    rows = build_command_center(org, user)["job_rows"]
    scores = [r["match"]["score"] for r in rows]
    assert len(rows) == 3
    assert all(isinstance(s, int) for s in scores)
    assert scores == sorted(scores, reverse=True)
    assert rows[0]["listing"].url.endswith("/2")


def test_recommendations_respect_min_prescore():
    user, org = _workspace()
    _profile(org, user, skills_primary=["django", "drf"], min_prescore=60)
    make_listing(url="https://remoteok.com/jobs/1", title="Cook", tags=[])
    make_listing(url="https://remoteok.com/jobs/2", tags=["Django", "DRF"])
    make_listing(url="https://remoteok.com/jobs/3", title="Django dev", tags=[])
    cc = build_command_center(org, user)
    recs = cc["recommendations"]
    assert [r["listing"].url[-1] for r in recs] == ["2"]
    assert all(r["match"]["score"] >= 60 for r in recs)
    assert len(cc["job_rows"]) == 3


def test_documents_summary_counts_by_kind_and_isolates_users():
    user, org = _workspace()
    _doc(org, user, DocumentKind.CV, 1)
    _doc(org, user, DocumentKind.CV, 2)
    _doc(org, user, DocumentKind.DIPLOMA, 3)
    other = make_user(email="other@example.com")
    _doc(org, other, DocumentKind.COVER_LETTER, 4)
    _doc(org, other, DocumentKind.CV, 5)
    summary = build_command_center(org, user)["documents_summary"]
    counts = {r["kind"]: r["count"] for r in summary}
    assert counts == {"cv": 2, "diploma": 1}
    assert all(r["label"] for r in summary)


def test_ingestion_runs_group_by_platform():
    user, org = _workspace()
    now = timezone.now()
    make_listing(url="https://remoteok.com/jobs/1", scraped_at=now)
    make_listing(url="https://remoteok.com/jobs/2", scraped_at=now - timedelta(days=2))
    make_listing(url="https://remoteok.com/jobs/3", scraped_at=now - timedelta(days=30))
    make_listing("wwr", url="https://wwr.com/1",
                 scraped_at=now - timedelta(days=1))
    runs = build_command_center(org, user)["ingestion_runs"]
    assert len(runs) == 2
    by = {r["platform"]: r for r in runs}
    assert sorted(r["new_count"] for r in runs) == [1, 2]
    assert runs[0]["last_scraped_at"] >= runs[1]["last_scraped_at"]
    assert max(by.values(), key=lambda r: r["new_count"])["new_count"] == 2


def test_empty_workspace_new_keys_empty():
    cc = build_command_center(None, make_user())
    for key in ("documents_summary", "job_rows", "recommendations", "ingestion_runs"):
        assert cc[key] == []
