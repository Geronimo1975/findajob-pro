"""Static-files storage that tolerates broken references in vendored CSS.

The Jobick dashboard theme (``apps/dashboard/static/dashboard_app/``) is a
third-party Bootstrap template whose CSS references demo images and source maps
that were not vendored (they are not used by any shipped screen). Django's
``ManifestStaticFilesStorage`` resolves *every* ``url(...)`` / ``@import`` it
finds while post-processing, and raises ``ValueError`` on the first missing one,
which aborts the whole ``collectstatic`` run — and with it the api container's
start-up (seen in prod 2026-10-18).

``ForgivingManifestStaticFilesStorage`` keeps the content-hashing (so nginx's
30-day cache on ``/static/`` still works for every real asset) but, when a
referenced file genuinely does not exist, leaves that one reference unhashed
instead of failing the run. Our own assets all resolve and hash normally; only
the unused demo references fall through, and those 404 harmlessly if ever hit.
"""

from __future__ import annotations

from django.contrib.staticfiles.storage import ManifestStaticFilesStorage


class ForgivingManifestStaticFilesStorage(ManifestStaticFilesStorage):
    #: Runtime ``{% static %}`` lookups of an unmapped name return the name as
    #: given rather than raising — the collectstatic forgiveness below has the
    #: same spirit at build time.
    manifest_strict = False

    def hashed_name(self, name, content=None, filename=None):
        try:
            return super().hashed_name(name, content=content, filename=filename)
        except ValueError:
            # A url()/@import target that was not collected (a vendored demo
            # image or an un-vendored source map). Leave the reference as-is.
            return name
