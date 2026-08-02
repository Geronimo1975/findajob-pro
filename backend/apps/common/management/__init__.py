"""Management commands owned by ``apps.common``.

``apps.common`` owns no models, so anything here is cross-cutting operational
tooling rather than app logic. Today that is exactly one command:
``check_login_flow`` (the daily end-to-end login healthcheck).
"""
