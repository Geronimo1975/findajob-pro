"""Serializers for the tenancy API.

:class:`MeSerializer` produces the shape pinned by implementation spec §3, byte
for byte::

    {"user":{"id":1,"email":"a@b.de","is_verified":true,"lang":"de"},
     "org":{"id":1,"name":"…","slug":"…"},
     "membership_role":"owner","memberships":[…],"lang":"de"}

``usage`` and ``flags`` are added later by ``entitlements-quotas`` — additive
only, so the shell never breaks.

Note on the user fields: ``apps.accounts`` owns ``User``, and this app must not
import it. Every user attribute is therefore read with ``getattr`` defaults, so
tenancy keeps working against whatever shape ``accounts.User`` settles on and a
missing field degrades to a sane value instead of a 500.
"""

from __future__ import annotations

from django.conf import settings
from rest_framework import serializers

from apps.tenancy.models import Membership, Organization


def normalise_lang(value) -> str:
    """Coerce anything into one of the supported language codes."""
    supported = getattr(settings, "SUPPORTED_LANGUAGE_CODES", ("en",))
    text = (str(value) if value else "").strip().lower()
    if text in supported:
        return text
    base = text.split("-", 1)[0]
    if base in supported:
        return base
    default = str(getattr(settings, "LANGUAGE_CODE", "en")).lower()
    return default if default in supported else supported[0]


class OrganizationSummarySerializer(serializers.ModelSerializer):
    """The ``org`` object in ``/api/v1/me/`` — id, name, slug. Nothing else."""

    class Meta:
        model = Organization
        fields = ("id", "name", "slug")
        read_only_fields = fields


class MembershipSummarySerializer(serializers.ModelSerializer):
    org = OrganizationSummarySerializer(read_only=True)

    class Meta:
        model = Membership
        fields = ("id", "role", "org")
        read_only_fields = fields


class UserSummarySerializer(serializers.Serializer):
    """The ``user`` object in ``/api/v1/me/``.

    Not a ``ModelSerializer``: binding to ``accounts.User`` would make tenancy
    depend on a module it is forbidden to import (spec §2).
    """

    def to_representation(self, user) -> dict:
        return {
            "id": user.pk,
            "email": getattr(user, "email", "") or "",
            "is_verified": bool(getattr(user, "is_verified", False)),
            "lang": normalise_lang(getattr(user, "lang", None)),
        }


class MeSerializer(serializers.Serializer):
    """The complete session payload.

    Context keys:

    ``memberships``  pre-fetched list of the user's memberships (required)
    ``membership``   the one this request is acting under, or ``None``
    """

    def to_representation(self, user) -> dict:
        memberships = list(self.context.get("memberships") or [])
        membership = self.context.get("membership")
        user_data = UserSummarySerializer(user).data
        return {
            "user": user_data,
            "org": (
                OrganizationSummarySerializer(membership.org).data
                if membership is not None
                else None
            ),
            "membership_role": membership.role if membership is not None else None,
            "memberships": MembershipSummarySerializer(memberships, many=True).data,
            "lang": user_data["lang"],
        }
