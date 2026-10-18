"""Org resolution for the server-rendered dashboard.

Unlike ``GET /api/v1/me/`` (403 ``not_org_member`` on a foreign ``?org=``), this
surface renders HTML and must always show *something* the user owns: a foreign
or malformed org reference falls back to the user's oldest membership. The
foreign org is never returned.
"""

from __future__ import annotations

from apps.tenancy.services import memberships_for


def resolve_org(request):
    """Return the Organization this request acts under, or ``None``."""
    rows = list(memberships_for(request.user))  # oldest first, org preloaded
    if not rows:
        return None

    ref = request.GET.get("org") or request.META.get("HTTP_X_FINDAJOB_ORG")
    try:
        wanted = int(ref) if ref not in (None, "") else None
    except (TypeError, ValueError):
        wanted = None

    if wanted is not None:
        for membership in rows:
            if membership.org_id == wanted:
                return membership.org
    return rows[0].org
