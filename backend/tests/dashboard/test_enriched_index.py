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
    assert "New" in jobs and "90k EUR" in jobs
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
