"""Input validation for the two anonymous auth endpoints.

These serializers do **only** validation and canonicalisation. They never touch
the database and never decide anything: the views map their field errors onto
the pinned machine codes from spec 3 (``invalid_email`` / ``invalid_code``) and
:mod:`apps.accounts.services` owns every rule that matters.

Canonicalisation happens here rather than in the service so there is exactly one
definition of "the same address" and "the same code" - a rate-limit budget keyed
on ``A@B.DE`` while the row is stored as ``a@b.de`` would be trivially bypassed
by pressing shift.
"""

from __future__ import annotations

import re
import unicodedata

from django.conf import settings
from rest_framework import serializers

from apps.accounts.models import MAX_EMAIL_LENGTH, OTP_CODE_LENGTH, normalize_email

#: Characters a user or their mail client may leave between the digits.
#:
#: Declared as integer codepoints and assembled with ``chr()`` rather than
#: written into a string literal. Neither a literal NBSP nor a ``\uXXXX`` escape
#: survives being copied through a plan, a diff or a chat window intact, and a
#: mangled character class fails *open*: the pasted code simply stops
#: validating, with no test able to see why. Integers cannot be mangled.
#:
#: 0x09/0x0A/0x0D/0x20 ASCII whitespace, 0xA0 NBSP, 0x2007 figure space,
#: 0x202F narrow NBSP (what Apple Mail's one-tap autofill inserts),
#: 0x2E/0x5F/0x2D full stop, underscore, hyphen-minus,
#: 0x2010..0x2015 the Unicode dashes.
_CODE_SEPARATOR_CODEPOINTS = (
    0x09, 0x0A, 0x0D, 0x20,
    0xA0, 0x2007, 0x202F,
    0x2E, 0x5F, 0x2D,
    0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015,
)

_CODE_SEPARATORS = re.compile(
    "["
    + re.escape("".join(chr(codepoint) for codepoint in _CODE_SEPARATOR_CODEPOINTS))
    + "]+"
)

_DIGITS_ONLY = re.compile(rf"\A[0-9]{{{OTP_CODE_LENGTH}}}\Z")

#: Generous upper bound before normalisation, purely to stop a megabyte of
#: "code" from being regex-scanned.
MAX_RAW_CODE_LENGTH = 64
MAX_TERMS_VERSION_LENGTH = 64


def normalise_lang(value) -> str:
    """Coerce anything into one of ``settings.LANGUAGES``.

    Lenient on purpose: ``lang`` is a hint about which of three translations to
    put in an email, not a credential, so ``"de-AT"`` should quietly become
    ``"de"`` rather than 400 a login.

    (``apps.tenancy.serializers`` carries an identical helper. Duplicated rather
    than imported: spec 2 makes the two apps deliberately independent, and eight
    lines is cheaper than a cross-app import that would have to be justified at
    every review.)
    """
    supported = getattr(settings, "SUPPORTED_LANGUAGE_CODES", ("en",))
    text = (str(value) if value else "").strip().lower()
    if text in supported:
        return text
    base = text.split("-", 1)[0]
    if base in supported:
        return base
    default = str(getattr(settings, "LANGUAGE_CODE", "en")).lower()
    return default if default in supported else supported[0]


class NormalizedEmailField(serializers.EmailField):
    """``EmailField`` that canonicalises *before* validating.

    Order matters: ``" George@Example.DE "`` must validate, and NFKC folding of
    lookalike codepoints has to happen before ``EmailValidator`` sees the value,
    not after - otherwise two spellings of one address could both be accepted
    and end up in different rate-limit buckets.
    """

    def to_internal_value(self, data):
        return super().to_internal_value(normalize_email(data))


class OtpCodeField(serializers.CharField):
    """The six digits, however the user's mail client mangled them."""

    def to_internal_value(self, data):
        text = super().to_internal_value(data)
        text = unicodedata.normalize("NFKC", text)
        text = _CODE_SEPARATORS.sub("", text)
        if not _DIGITS_ONLY.match(text):
            raise serializers.ValidationError(
                f"Enter the {OTP_CODE_LENGTH}-digit code from the email."
            )
        return text


class RequestCodeSerializer(serializers.Serializer):
    """``POST /api/v1/auth/request-code/`` - ``{email, lang}`` (spec 3)."""

    email = NormalizedEmailField(max_length=MAX_EMAIL_LENGTH)
    lang = serializers.CharField(
        required=False, allow_blank=True, max_length=16, default=""
    )

    def validate_lang(self, value) -> str:
        # Blank stays blank. Normalising "" to the default would destroy the
        # difference between "the client chose English" and "the client said
        # nothing", and `apps.accounts.services.effective_lang` needs that
        # difference: only an explicit choice may overwrite a stored preference.
        text = (value or "").strip()
        return normalise_lang(text) if text else ""


class VerifyCodeSerializer(serializers.Serializer):
    """``POST /api/v1/auth/verify-code/`` - ``{email, code, accepts_terms}``.

    ``lang`` and ``terms_version`` are additive and optional. ``terms_version``
    is used **only** to detect staleness (spec 10): the binding document is
    resolved server-side by ``apps.legal``, and a client-supplied version string
    is never trusted as the record of what was agreed to.
    """

    email = NormalizedEmailField(max_length=MAX_EMAIL_LENGTH)
    code = OtpCodeField(max_length=MAX_RAW_CODE_LENGTH, trim_whitespace=True)
    accepts_terms = serializers.BooleanField(required=False, default=False)
    #: Opt-in, never automatic (trusted-device spec §6): marking a library or
    #: internet-cafe machine as trusted without being asked is exactly what this
    #: checkbox exists to prevent.
    remember_device = serializers.BooleanField(required=False, default=False)
    lang = serializers.CharField(
        required=False, allow_blank=True, max_length=16, default=""
    )
    terms_version = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=MAX_TERMS_VERSION_LENGTH,
        default="",
    )

    def validate_lang(self, value) -> str:
        # Blank stays blank. Normalising "" to the default would destroy the
        # difference between "the client chose English" and "the client said
        # nothing", and `apps.accounts.services.effective_lang` needs that
        # difference: only an explicit choice may overwrite a stored preference.
        text = (value or "").strip()
        return normalise_lang(text) if text else ""
