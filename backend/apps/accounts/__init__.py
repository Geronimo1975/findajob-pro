"""Auth identity — User, EmailOTP, SuppressedAddress, and the /api/v1/auth/* views.

Owned by a separate work stream (build-order position 3). This package is
intentionally empty apart from the migrations package: `AUTH_USER_MODEL` is
already pinned to "accounts.User" in `config.settings.base`, so `models.py` must
define exactly that model before anything can migrate.

Module boundary (implementation spec §2): this app owns **exactly two** models,
`User` and `EmailOTP`, plus `SuppressedAddress`. It must not define, import or
instantiate `Organization`, `Membership` or `AuditLog` — those are
`apps.tenancy`, reachable only through `apps.tenancy.services.bootstrap_workspace()`.
"""
