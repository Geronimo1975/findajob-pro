"""Core tenancy: Organization, Membership, AuditLog.

Build-order position 1. Owns the single session endpoint ``GET /api/v1/me/``
and ``bootstrap_workspace()``, which ``apps.accounts`` calls inside the same
transaction that creates a user.

This app must never import from ``apps.accounts`` at module level — the
dependency runs the other way. ``settings.AUTH_USER_MODEL`` and
``get_user_model()`` are the only permitted references to the user.
"""
