"""Input validation for ``POST /api/v1/auth/device-login/``.

Kept in its own module rather than added to :mod:`apps.accounts.serializers`,
whose docstring pins it to *"the two anonymous auth endpoints"* from auth spec
§3. Trusted devices are an additive subsystem with their own spec
(``docs/2026-08-22-trusted-device-spec.md``), and mixing them in would blur a
boundary the auth spec draws deliberately.
"""

from __future__ import annotations

from rest_framework import serializers

from apps.accounts.models import MAX_EMAIL_LENGTH
from apps.accounts.serializers import NormalizedEmailField


class DeviceLoginSerializer(serializers.Serializer):
    """``{email}`` — the cookie carries everything else."""

    email = NormalizedEmailField(max_length=MAX_EMAIL_LENGTH)
