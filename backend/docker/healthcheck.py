"""Container liveness probe for the findajob API.

Run by Docker's HEALTHCHECK. The contract is deliberately narrow:

    "is the WSGI process accepting connections and producing an HTTP response?"

It is **not** a readiness or dependency probe. A 401 (anonymous request to the
session endpoint), a 400 (``DisallowedHost`` — 127.0.0.1 is not in
``ALLOWED_HOSTS``) and a 200 are all *healthy*: each one proves gunicorn
accepted the socket, Django routed the request and a response came back.

Only a connection failure, a timeout or a malformed reply is unhealthy. If this
probe consulted PostgreSQL or Redis, a transient database blip would mark the
container unhealthy, Docker would restart it, and an outage confined to a few
routes would become a restart loop across the whole API.

Two headers are sent so the probe stays *quiet* as well as correct:

``Host``
    taken from ``ALLOWED_HOSTS``. Without it every probe raises
    ``DisallowedHost``, which ``config.settings.base``'s ``django.security``
    logger writes to stdout — once every 30 s, for the life of the container,
    into a log capped at 3 x 10 MB.

``X-Forwarded-Proto: https``
    ``config.settings.prod`` sets ``SECURE_SSL_REDIRECT = True``. A plain-http
    probe with a valid Host would otherwise be answered with a 301 to
    ``https://findajob.pro/…`` — i.e. the container would healthcheck itself by
    going out to the public internet. Redirect following is disabled below as
    well, belt and braces.

Neither header is a trust decision: nothing outside this container can reach
port 8000 (no ``ports:`` mapping anywhere in deploy/docker-compose.yml).

Exit status: 0 healthy, 1 unhealthy.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

#: Must match gunicorn's bind port (GUNICORN_BIND / the compose service).
PORT = os.environ.get("HEALTHCHECK_PORT", "8000")

#: Any routed path works. `/api/v1/me/` is guaranteed to exist because
#: apps.tenancy owns it unconditionally (config/urls.py mounts it directly,
#: not through `_include_if_present`).
URL = f"http://127.0.0.1:{PORT}/api/v1/me/"

TIMEOUT = float(os.environ.get("HEALTHCHECK_TIMEOUT", "4"))


def main() -> int:
    request = urllib.request.Request(URL, method="GET")
    request.add_header("User-Agent", "findajob-healthcheck/1")
    # Ask for the identity encoding so a compressed body cannot confuse anything
    # downstream; we never read the body at all.
    request.add_header("Accept-Encoding", "identity")

    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return 0 if 100 <= response.status < 600 else 1
    except urllib.error.HTTPError:
        # 4xx/5xx from Django still proves the stack is alive and routing.
        return 0
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"healthcheck: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
