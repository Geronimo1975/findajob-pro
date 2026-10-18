"""Lazy profile creation. Deliberately NOT called from ``bootstrap_workspace``."""

from __future__ import annotations

from apps.profiles.models import SearchProfile, UserProfile


def get_or_create_search_profile(org, user) -> SearchProfile:
    profile, _ = SearchProfile.all_objects.get_or_create(org=org, user=user)
    return profile


def get_or_create_user_profile(org, user) -> UserProfile:
    profile, _ = UserProfile.all_objects.get_or_create(org=org, user=user)
    return profile
