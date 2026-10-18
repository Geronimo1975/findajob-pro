"""Enriched command center: documents, recommendations, jobs table, automation."""

import re

import pytest
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client

from apps.jobs.models import Document, DocumentKind
from apps.profiles.services import get_or_create_search_profile
from apps.tenancy.services import bootstrap_workspace
from tests.jobs.test_models import make_listing
from tests.support import make_user

pytestmark = pytest.mark.django_db


def _login():
    call_command("seed_platforms", verbosity=0)
    user = make_user("enriched@example.com")
    org = bootstrap_workspace(user).org
    client = Client()
    client.force_login(user)
    return client, user, org


def _section(html, marker):
    m = re.search(rf'data-block="{marker}".*?(?=data-block=|</body>)', html, re.S)
    assert m, f"block {marker} missing"
    return m.group(0)


def test_enriched_blocks_render_seeded_data():
    client, user, org = _login()
    p = get_or_create_search_profile(org, user)
    p.skills_primary = ["django", "drf"]
    p.min_prescore = 50
    p.save()
    make_listing(url="https://remoteok.com/jobs/1", title="Zephyr Django Engineer",
                 tags=["Django", "DRF"], salary="90k EUR")
    Document.objects.create(org=org, user=user, name="cv", kind=DocumentKind.CV,
                            file=ContentFile(b"x", name="cv.pdf"))
    html = client.get("/app/").content.decode()

    jobs = _section(html, "jobs-table")
    assert "Zephyr Django Engineer" in jobs
    assert re.search(r"\d+%", jobs)
    assert "Not applied" in jobs and "90k EUR" in jobs
    assert ">New<" not in jobs and 'href="#"' not in jobs
    assert 'href="https://remoteok.com/jobs/1"' in jobs
    assert "overflow-x:auto" in html.replace(" ", "")

    docs = _section(html, "documents")
    assert "CV" in docs and ">1<" in docs.replace(" ", "").replace("\n", "")

    assert "Zephyr Django Engineer" in _section(html, "recommendations")

    auto = _section(html, "automation")
    assert "RemoteOK" in auto or "remoteok" in auto.lower()
    assert ">1<" in auto.replace(" ", "").replace("\n", "")


def test_empty_workspace_renders_empty_states():
    client, user, org = _login()
    resp = client.get("/app/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "No documents yet" in _section(html, "documents")
    assert "No matches yet" in _section(html, "recommendations")
    assert "No ingestion runs yet" in _section(html, "automation")
    assert "No jobs yet" in _section(html, "jobs-table")


def _row(jobs_html, title):
    m = re.search(rf"<tr>(?:(?!</tr>).)*{re.escape(title)}.*?</tr>", jobs_html, re.S)
    assert m, title
    return m.group(0)


def test_status_reflects_real_application_and_score_pill_threshold():
    from apps.dashboard.kpis import build_command_center
    from apps.jobs.models import Application, ApplicationStatus as S

    client, user, org = _login()
    p = get_or_create_search_profile(org, user)
    p.skills_primary = ["django", "drf"]
    p.min_prescore = 60
    p.save()
    hi = make_listing(url="https://remoteok.com/jobs/1", title="Alpha Django Lead",
                      tags=["Django", "DRF"])
    make_listing(url="https://remoteok.com/jobs/2", title="Beta Pastry Chef", tags=[])
    Application.objects.create(org=org, user=user, job_listing=hi, status=S.SENT)

    rows = {r["listing"].title: r["match"]["score"]
            for r in build_command_center(org, user)["job_rows"]}
    assert rows["Alpha Django Lead"] >= 60 > rows["Beta Pastry Chef"]

    jobs = _section(client.get("/app/").content.decode(), "jobs-table")
    a, b = _row(jobs, "Alpha Django Lead"), _row(jobs, "Beta Pastry Chef")
    assert f'{rows["Alpha Django Lead"]}%' in a and "fj-score-hi" in a
    assert f'{rows["Beta Pastry Chef"]}%' in b and "fj-score-hi" not in b
    assert str(S.SENT.label) in a and "Not applied" not in a
    assert "Not applied" in b
