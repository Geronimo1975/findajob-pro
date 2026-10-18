"""Profiles models — matcher config shape, org scoping, lazy create."""

from __future__ import annotations

from types import SimpleNamespace

from django.db import IntegrityError, transaction
from django.test import TestCase

from apps.jobs.matching import HARD_REJECT, prescore, skill_match
from apps.profiles.models import SearchProfile, UserProfile
from apps.profiles.services import (
    get_or_create_search_profile,
    get_or_create_user_profile,
)
from apps.tenancy.managers import OrgScopeError
from apps.tenancy.services import bootstrap_workspace
from tests.support import make_user


class ProfileFixture(TestCase):
    def setUp(self):
        self.user = make_user("cand@example.de")
        self.org = bootstrap_workspace(self.user).org


class MatchingConfigTests(ProfileFixture):
    def make_profile(self):
        return SearchProfile.all_objects.create(
            org=self.org, user=self.user,
            skills_primary=["Python", "Django"],
            skills_secondary=["Docker"],
            keyword_groups=[["Python Developer"], ["Backend Engineer"]],
            negative_keywords=["Sales"],
            exclude_keywords=["VP"],
            flex_signals=["remote"],
            min_prescore=40,
        )

    def test_config_shape(self):
        cfg = self.make_profile().to_matching_config()
        self.assertEqual(cfg["skills"], {"primary": ["Python", "Django"], "secondary": ["Docker"]})
        filters = cfg["scraping"]["filters"]
        self.assertEqual(filters["negative_keywords"], ["Sales"])
        self.assertEqual(filters["exclude_keywords"], ["VP"])
        self.assertEqual(filters["flex_signals"], ["remote"])
        self.assertEqual(filters["min_prescore"], 40)
        self.assertEqual(
            cfg["scraping"]["markets"]["profile"]["keyword_groups"],
            [["Python Developer"], ["Backend Engineer"]],
        )

    def test_prescore_consumes_config(self):
        cfg = self.make_profile().to_matching_config()
        score, reasons = prescore(
            {"title": "Python Developer", "description": "Django, remote"}, cfg)
        self.assertGreater(score, 0)
        self.assertIn("Python", reasons)
        self.assertIn("cautare: Python Developer", reasons)
        self.assertIn("flexibil: remote", reasons)
        self.assertEqual(prescore({"title": "VP Engineering"}, cfg)[0], HARD_REJECT)

    def test_skill_match_consumes_profile(self):
        listing = SimpleNamespace(title="Django dev", description="Python and Docker",
                                  tags=[], requirements="")
        result = skill_match(listing, self.make_profile())
        self.assertEqual(result["score"], 100)
        self.assertEqual(result["missing"], [])

    def test_empty_profile_yields_empty_skills(self):
        cfg = get_or_create_search_profile(self.org, self.user).to_matching_config()
        self.assertEqual(cfg["skills"], {"primary": [], "secondary": []})
        self.assertEqual(cfg["scraping"]["filters"]["min_prescore"], 30)


class ScopingTests(ProfileFixture):
    def test_unscoped_evaluation_raises(self):
        for model in (SearchProfile, UserProfile):
            with self.assertRaises(OrgScopeError):
                list(model.objects.all())

    def test_for_user_works(self):
        get_or_create_search_profile(self.org, self.user)
        self.assertEqual(SearchProfile.objects.for_user(self.user).count(), 1)

    def test_unique_per_org_user(self):
        SearchProfile.all_objects.create(org=self.org, user=self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SearchProfile.all_objects.create(org=self.org, user=self.user)


class LazyCreateTests(ProfileFixture):
    def test_search_profile_created_once(self):
        a = get_or_create_search_profile(self.org, self.user)
        b = get_or_create_search_profile(self.org, self.user)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(SearchProfile.all_objects.count(), 1)

    def test_user_profile_created_once(self):
        a = get_or_create_user_profile(self.org, self.user)
        b = get_or_create_user_profile(self.org, self.user)
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(UserProfile.all_objects.count(), 1)

    def test_bootstrap_does_not_create_profiles(self):
        self.assertEqual(SearchProfile.all_objects.count(), 0)
        self.assertEqual(UserProfile.all_objects.count(), 0)
