"""Session authentication that answers 401 instead of 403.

DRF picks the status code for an unauthenticated request from the *first*
authentication class: if ``authenticate_header()`` returns ``None`` it downgrades
401 to 403. Plain ``SessionAuthentication`` returns ``None``, so an anonymous
call to ``GET /api/v1/me/`` would come back 403 — and spec §7 says the shell
boots by calling ``/api/v1/me/`` and redirects to ``/auth.html`` on **401**.

CSRF behaviour is inherited unchanged: ``SessionAuthentication.enforce_csrf``
still rejects unsafe methods that arrive without a valid ``X-CSRFToken`` on a
cookie-authenticated request. That is the second half of spec §8 — the first
half is the explicit ``@csrf_protect`` on the two anonymous POST views, which
DRF cannot supply because ``APIView.as_view()`` is ``csrf_exempt``.
"""

from __future__ import annotations

from rest_framework.authentication import SessionAuthentication


class CookieSessionAuthentication(SessionAuthentication):
    """``SessionAuthentication`` + a ``WWW-Authenticate`` header."""

    def authenticate_header(self, request) -> str:
        # Any non-None value makes DRF keep the 401. "Session" is not an IANA
        # scheme, which is deliberate: naming a real scheme such as "Basic"
        # would make browsers pop a native credentials dialog.
        return 'Session realm="findajob", charset="UTF-8"'
