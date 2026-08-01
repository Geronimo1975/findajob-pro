"""Application packages.

``apps.common``   — shared plumbing with no models (error envelope, auth class).
``apps.tenancy``  — Organization, Membership, AuditLog, ``GET /api/v1/me/``.
``apps.accounts`` — User, EmailOTP, SuppressedAddress and the ``/auth/*`` views.
``apps.legal``    — LegalDocument, ConsentRecord.

Module boundaries are hard (implementation spec §2): ``apps.accounts`` must not
define, import or instantiate ``Organization``, ``Membership`` or ``AuditLog``,
and nothing here may reference ``Invitation``, ``Subscription`` or ``UsageQuota``
— those belong to build-order positions 5–7, which are not built.
"""
