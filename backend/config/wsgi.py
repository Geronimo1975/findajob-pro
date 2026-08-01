"""WSGI entrypoint — this is what gunicorn imports in production.

``gunicorn config.wsgi:application --bind 0.0.0.0:8000``

``setdefault`` means an explicit ``DJANGO_SETTINGS_MODULE`` in the environment
always wins; the default is the production module because gunicorn is only ever
used in production.
"""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_wsgi_application()
