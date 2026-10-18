"""Candidate profile and job-search profile. Both org-scoped (spec: pipeline-port §4).

Contact email is ``user.email`` and is deliberately not duplicated here.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.tenancy.managers import OrgScopedManager


class Tone(models.TextChoices):
    FORMAL = "formal", _("formal")
    FRIENDLY = "friendly", _("friendly")
    DIRECT = "direct", _("direct")


class PreferredLanguage(models.TextChoices):
    DE = "de", "Deutsch"
    EN = "en", "English"


class UserProfile(models.Model):
    org = models.ForeignKey(
        "tenancy.Organization", on_delete=models.CASCADE,
        related_name="user_profiles", verbose_name=_("organization"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="user_profiles", verbose_name=_("user"),
    )
    full_name = models.CharField(_("full name"), max_length=255, blank=True)
    headline = models.CharField(_("headline"), max_length=255, blank=True)
    phone = models.CharField(_("phone"), max_length=64, blank=True)
    location = models.CharField(_("location"), max_length=255, blank=True)
    linkedin_url = models.URLField(_("LinkedIn URL"), blank=True)
    github_url = models.URLField(_("GitHub URL"), blank=True)
    years_experience = models.PositiveSmallIntegerField(
        _("years of experience"), null=True, blank=True)
    tone = models.CharField(
        _("tone"), max_length=16, choices=Tone.choices, default=Tone.FORMAL)
    summary = models.TextField(_("summary"), blank=True)
    preferred_language = models.CharField(
        _("preferred language"), max_length=2,
        choices=PreferredLanguage.choices, default=PreferredLanguage.DE)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = OrgScopedManager(user_field="user")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("user profile")
        verbose_name_plural = _("user profiles")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "user"], name="uniq_user_profile_org_user"),
        ]

    def __str__(self) -> str:
        return self.full_name or f"profile {self.pk}"


class SearchProfile(models.Model):
    org = models.ForeignKey(
        "tenancy.Organization", on_delete=models.CASCADE,
        related_name="search_profiles", verbose_name=_("organization"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="search_profiles", verbose_name=_("user"),
    )
    skills_primary = models.JSONField(_("primary skills"), default=list, blank=True)
    skills_secondary = models.JSONField(_("secondary skills"), default=list, blank=True)
    #: list[list[str]] — title-bonus search keywords, grouped as Job_app markets.
    keyword_groups = models.JSONField(_("keyword groups"), default=list, blank=True)
    negative_keywords = models.JSONField(_("negative keywords"), default=list, blank=True)
    exclude_keywords = models.JSONField(_("exclude keywords"), default=list, blank=True)
    flex_signals = models.JSONField(_("flexibility signals"), default=list, blank=True)
    min_prescore = models.PositiveSmallIntegerField(_("minimum prescore"), default=30)
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = OrgScopedManager(user_field="user")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("search profile")
        verbose_name_plural = _("search profiles")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "user"], name="uniq_search_profile_org_user"),
        ]

    def __str__(self) -> str:
        return f"search profile {self.pk}"

    def to_matching_config(self) -> dict:
        """The Job_app config shape ``apps.jobs.matching`` consumes.

        The profile's keyword groups live under a single pseudo-market. Jobs
        carry no matching ``market`` key, so ``_market_keywords`` falls back to
        the union of all markets, i.e. exactly these groups.
        """
        return {
            "skills": {
                "primary": list(self.skills_primary or []),
                "secondary": list(self.skills_secondary or []),
            },
            "scraping": {
                "filters": {
                    "negative_keywords": list(self.negative_keywords or []),
                    "exclude_keywords": list(self.exclude_keywords or []),
                    "flex_signals": list(self.flex_signals or []),
                    "min_prescore": self.min_prescore,
                },
                "markets": {
                    "profile": {
                        "keyword_groups": [list(g) for g in (self.keyword_groups or [])],
                    },
                },
            },
        }
