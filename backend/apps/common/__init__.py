"""Shared, model-free plumbing used by every other app.

* ``apps.common.errors``         — the ``{"error": {...}}`` envelope, ``ApiError``
                                    and the pinned machine codes.
* ``apps.common.authentication`` — session auth that answers 401, not 403.
* ``apps.common.logging``        — the redacting log filter.
"""
