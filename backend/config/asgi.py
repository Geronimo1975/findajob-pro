"""ASGI entrypoint.

Not used by the current gunicorn/WSGI deployment, but kept correct so switching
to an ASGI server (uvicorn workers) is a one-line change in the container CMD.
"""

from __future__ import annotations

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

application = get_asgi_application()
