"""A thin, read-only n8n REST client.

Design constraints, all of them load-bearing:

* **Standard library only.** ``urllib.request`` — no new dependency. ``requests``
  is present but only as a transitive pin of ``django-anymail``; depending on it
  directly would promote a transitive into a direct dependency for four lines of
  convenience.
* **Never raises into a view.** Every public method returns an :class:`N8nResult`
  whose ``ok`` flag and ``error_kind`` describe the failure. A missing token, a
  dead host, a 401, a 20-second hang and a malformed body all come back as data.
  The panel therefore degrades to a message instead of a 500.
* **Hard timeout.** ``settings.N8N_TIMEOUT`` (5 s) is passed to ``urlopen`` and
  bounds connect *and* read.
* **Cached.** ``settings.N8N_CACHE_TTL`` (60 s) so opening the admin repeatedly
  does not hammer the API. Failures are cached for a shorter
  ``N8N_CACHE_ERROR_TTL`` (15 s), which is what stops a dead n8n from turning
  every admin page load into a 5-second stall.
* **Projected before caching.** ``GET /workflows`` returns the full node graph of
  every workflow — measured at 2.65 MB for this instance's 39 workflows. Only the
  handful of scalar fields the panel renders are kept, so what lands in Redis is
  a few kilobytes.
* **Read-only.** ``GET`` is the only method this module can issue. There is no
  code path here that writes to n8n.

The token is a full-privilege n8n API key: it is never logged, never rendered,
never put in a cache key in the clear, and never included in an error string.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger("findajob.automation")

#: Sent on every request. n8n authenticates API keys with this header, not with
#: ``Authorization``.
API_KEY_HEADER = "X-N8N-API-KEY"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect while carrying the n8n API token.

    ``HTTPRedirectHandler.redirect_request`` copies every header except
    content-length/content-type onto the new request, so a 302 from the n8n host
    to anywhere else would hand ``X-N8N-API-KEY`` to that host. This client only
    ever calls two known read-only endpoints on a configured host; a redirect is
    never a legitimate answer, so returning None turns it into an HTTPError the
    caller already handles.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


#: Built once. ``urlopen`` uses the global opener, which installs the redirect
#: handler we are refusing here.
_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)

USER_AGENT = "findajob-admin/1.0 (+https://findajob.pro)"

#: Refuse to buffer more than this from a single response. The workflows payload
#: is legitimately a few megabytes; anything past 32 MB is a bug or an attack.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

#: Only these schemes may be dialled. Guards against an ``N8N_URL`` of
#: ``file://…`` and against a ``javascript:`` value reaching an ``href``.
ALLOWED_SCHEMES = frozenset({"http", "https"})


# ---------------------------------------------------------------------------
# Typed results
# ---------------------------------------------------------------------------

class ErrorKind:
    """Machine-readable failure categories. ``""`` means success."""

    NONE = ""
    NOT_CONFIGURED = "not_configured"
    TIMEOUT = "timeout"
    HTTP = "http"
    NETWORK = "network"
    DECODE = "decode"
    UNEXPECTED = "unexpected"
    #: Only ever returned for a ``cached_only=True`` call — "nothing warm yet",
    #: which is a normal state, not an error to surface to the operator.
    CACHE_MISS = "cache_miss"


@dataclass(frozen=True, slots=True)
class N8nWorkflow:
    """One workflow, projected down to what the panel renders."""

    id: str
    name: str
    active: bool
    node_count: int
    trigger_count: int
    is_archived: bool
    created_at: datetime | None
    updated_at: datetime | None
    tags: tuple[str, ...] = ()

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "N8nWorkflow":
        nodes = raw.get("nodes")
        tags = raw.get("tags") or []
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or "(unnamed)"),
            active=bool(raw.get("active")),
            node_count=len(nodes) if isinstance(nodes, list) else 0,
            trigger_count=_as_int(raw.get("triggerCount")),
            is_archived=bool(raw.get("isArchived")),
            created_at=parse_timestamp(raw.get("createdAt")),
            updated_at=parse_timestamp(raw.get("updatedAt")),
            tags=tuple(
                str(t.get("name")) for t in tags
                if isinstance(t, dict) and t.get("name")
            ),
        )

    def as_cacheable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "active": self.active,
            "node_count": self.node_count,
            "trigger_count": self.trigger_count,
            "is_archived": self.is_archived,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "tags": list(self.tags),
        }

    @classmethod
    def from_cacheable(cls, raw: dict[str, Any]) -> "N8nWorkflow":
        return cls(
            id=raw["id"],
            name=raw["name"],
            active=raw["active"],
            node_count=raw["node_count"],
            trigger_count=raw["trigger_count"],
            is_archived=raw["is_archived"],
            created_at=parse_timestamp(raw["created_at"]),
            updated_at=parse_timestamp(raw["updated_at"]),
            tags=tuple(raw.get("tags") or ()),
        )


@dataclass(frozen=True, slots=True)
class N8nExecution:
    """One execution of a workflow."""

    id: str
    workflow_id: str
    status: str
    mode: str
    started_at: datetime | None
    stopped_at: datetime | None
    finished: bool

    #: n8n statuses that mean "this run did not do its job".
    FAILED_STATUSES = frozenset({"error", "crashed", "canceled", "cancelled"})

    @property
    def is_failure(self) -> bool:
        return self.status.lower() in self.FAILED_STATUSES

    @property
    def is_success(self) -> bool:
        return self.status.lower() == "success"

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "N8nExecution":
        return cls(
            id=str(raw.get("id") or ""),
            workflow_id=str(raw.get("workflowId") or ""),
            status=str(raw.get("status") or "unknown"),
            mode=str(raw.get("mode") or ""),
            started_at=parse_timestamp(raw.get("startedAt")),
            stopped_at=parse_timestamp(raw.get("stoppedAt")),
            finished=bool(raw.get("finished")),
        )

    def as_cacheable(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "mode": self.mode,
            "started_at": _iso(self.started_at),
            "stopped_at": _iso(self.stopped_at),
            "finished": self.finished,
        }

    @classmethod
    def from_cacheable(cls, raw: dict[str, Any]) -> "N8nExecution":
        return cls(
            id=raw["id"],
            workflow_id=raw["workflow_id"],
            status=raw["status"],
            mode=raw["mode"],
            started_at=parse_timestamp(raw["started_at"]),
            stopped_at=parse_timestamp(raw["stopped_at"]),
            finished=raw["finished"],
        )


@dataclass(frozen=True, slots=True)
class N8nResult:
    """What every public client method returns. Never an exception."""

    ok: bool
    workflows: tuple[N8nWorkflow, ...] = ()
    executions: tuple[N8nExecution, ...] = ()
    error: str = ""
    error_kind: str = ErrorKind.NONE
    status_code: int | None = None
    from_cache: bool = False
    fetched_at: datetime | None = None

    @property
    def not_configured(self) -> bool:
        return self.error_kind == ErrorKind.NOT_CONFIGURED

    @classmethod
    def failure(
        cls,
        kind: str,
        message: str,
        *,
        status_code: int | None = None,
        from_cache: bool = False,
    ) -> "N8nResult":
        return cls(
            ok=False,
            error=message,
            error_kind=kind,
            status_code=status_code,
            from_cache=from_cache,
            fetched_at=_now(),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an n8n ISO-8601 timestamp into an aware datetime.

    n8n emits ``2026-08-01T17:00:00.073Z``. ``fromisoformat`` accepts the ``Z``
    suffix from Python 3.11 onwards; the explicit fallback keeps the parser
    honest for a naive value, which must become UTC-aware or ``USE_TZ=True``
    warns on every render.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cache_get(key: str) -> Any:
    """Read the cache, treating a broken cache backend as a miss.

    Redis is cache-only in this project and a Redis outage must never be able to
    take a page down (``config/settings/base.py``).
    """
    try:
        return cache.get(key)
    except Exception:  # pragma: no cover - only reachable with a broken backend
        logger.warning("n8n panel: cache read failed; treating as a miss")
        return None


def _cache_set(key: str, value: Any, ttl: int) -> None:
    try:
        cache.set(key, value, ttl)
    except Exception:  # pragma: no cover - only reachable with a broken backend
        logger.warning("n8n panel: cache write failed; continuing uncached")


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class N8nClient:
    """Read-only n8n REST client. Construct via :func:`get_client`."""

    base_url: str = ""
    token: str = field(default="", repr=False)
    timeout: float = 5.0
    cache_ttl: int = 60
    error_cache_ttl: int = 15

    # -- configuration ----------------------------------------------------

    @property
    def is_configured(self) -> bool:
        return not self._configuration_problem()

    def _configuration_problem(self) -> str:
        if not self.base_url:
            return "N8N_URL is not set."
        if not self.token:
            return "N8N_API_TOKEN is not set."
        scheme = urllib.parse.urlsplit(self.base_url).scheme.lower()
        if scheme not in ALLOWED_SCHEMES:
            return (
                f"N8N_URL must use http:// or https://, got {scheme or 'no'} scheme."
            )
        if not urllib.parse.urlsplit(self.base_url).netloc:
            return "N8N_URL has no host."
        return ""

    @property
    def api_root(self) -> str:
        return self.base_url.rstrip("/") + "/api/v1"

    def workflow_url(self, workflow_id: str) -> str:
        """The human-facing n8n editor URL for a workflow.

        Safe to put in an ``href``: the scheme was validated in
        :meth:`_configuration_problem` and the id is percent-quoted.
        """
        if not self.is_configured or not workflow_id:
            return ""
        return f"{self.base_url.rstrip('/')}/workflow/{urllib.parse.quote(workflow_id, safe='')}"

    # -- public API -------------------------------------------------------

    def workflows(self, *, limit: int = 100, cached_only: bool = False) -> N8nResult:
        """``GET /api/v1/workflows``.

        ``cached_only=True`` returns the cached answer or a miss — it never
        touches the network. The admin index card uses it so rendering the index
        can never block on n8n.
        """
        params = {
            "limit": max(1, min(int(limit), 250)),
            # The panel shows counts, not payloads; pinned data is pure weight.
            "excludePinnedData": "true",
        }
        payload = self._get("/workflows", params, cached_only=cached_only)
        if isinstance(payload, N8nResult):
            return payload
        body, from_cache, fetched_at = payload

        if from_cache:
            items = tuple(
                N8nWorkflow.from_cacheable(row) for row in body.get("workflows", [])
            )
        else:
            raw_items = body.get("data")
            if not isinstance(raw_items, list):
                return N8nResult.failure(
                    ErrorKind.DECODE,
                    "n8n returned a body without a 'data' list.",
                )
            items = tuple(
                N8nWorkflow.from_api(row) for row in raw_items if isinstance(row, dict)
            )
            self._store("/workflows", params, {"workflows": [w.as_cacheable() for w in items]})

        return N8nResult(
            ok=True, workflows=items, from_cache=from_cache, fetched_at=fetched_at
        )

    def executions(
        self, workflow_id: str, *, limit: int = 5, cached_only: bool = False
    ) -> N8nResult:
        """``GET /api/v1/executions?workflowId=<id>&limit=<n>``."""
        if not workflow_id:
            return N8nResult.failure(ErrorKind.DECODE, "No workflow id given.")
        params = {
            "workflowId": workflow_id,
            "limit": max(1, min(int(limit), 100)),
            # We render status + timestamps only; the run data can be enormous.
            "includeData": "false",
        }
        payload = self._get("/executions", params, cached_only=cached_only)
        if isinstance(payload, N8nResult):
            return payload
        body, from_cache, fetched_at = payload

        if from_cache:
            items = tuple(
                N8nExecution.from_cacheable(row) for row in body.get("executions", [])
            )
        else:
            raw_items = body.get("data")
            if not isinstance(raw_items, list):
                return N8nResult.failure(
                    ErrorKind.DECODE,
                    "n8n returned a body without a 'data' list.",
                )
            items = tuple(
                N8nExecution.from_api(row) for row in raw_items if isinstance(row, dict)
            )
            self._store(
                "/executions", params, {"executions": [e.as_cacheable() for e in items]}
            )

        return N8nResult(
            ok=True, executions=items, from_cache=from_cache, fetched_at=fetched_at
        )

    # -- transport --------------------------------------------------------

    def _cache_key(self, path: str, params: dict[str, Any]) -> str:
        """Namespace by a *digest* of the credentials, never the credentials.

        Including the token digest means rotating the key, or repointing
        ``N8N_URL`` at a different instance, cannot serve the previous
        instance's cached answer.
        """
        identity = hashlib.sha256(
            f"{self.base_url}\x00{self.token}".encode("utf-8")
        ).hexdigest()[:16]
        query = urllib.parse.urlencode(sorted(params.items()))
        return f"findajob:n8n:{identity}:{path}?{query}"

    def _store(self, path: str, params: dict[str, Any], value: dict[str, Any]) -> None:
        entry = dict(value)
        # Lets a cache hit still say *when* the data was really fetched, which
        # is the difference between "n8n is fine" and "this is 59 s stale".
        entry["__fetched_at__"] = _iso(_now())
        _cache_set(self._cache_key(path, params), entry, self.cache_ttl)

    def _get(
        self, path: str, params: dict[str, Any], *, cached_only: bool
    ) -> tuple[dict[str, Any], bool, datetime | None] | N8nResult:
        """Fetch ``path``, or return a failed :class:`N8nResult`.

        Returns ``(body, from_cache, fetched_at)`` on success. The union return
        type keeps both callers above short; neither ever sees an exception.
        """
        problem = self._configuration_problem()
        if problem:
            return N8nResult.failure(ErrorKind.NOT_CONFIGURED, problem)

        key = self._cache_key(path, params)
        cached = _cache_get(key)
        if isinstance(cached, dict):
            if "__error__" in cached:
                stored = cached["__error__"]
                return N8nResult.failure(
                    stored.get("kind", ErrorKind.UNEXPECTED),
                    stored.get("message", "n8n request failed."),
                    status_code=stored.get("status_code"),
                    from_cache=True,
                )
            return cached, True, parse_timestamp(cached.get("__fetched_at__"))

        if cached_only:
            # Not a failure worth shouting about: the caller explicitly asked
            # for "whatever is already warm", and nothing is.
            return N8nResult.failure(
                ErrorKind.CACHE_MISS, "No cached n8n data yet."
            )

        url = f"{self.api_root}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(
            url,
            method="GET",  # read-only, by construction
            headers={
                API_KEY_HEADER: self.token,
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )

        try:
            with _NO_REDIRECT_OPENER.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # 401/403 is by far the likeliest: a revoked or mistyped API key.
            return self._fail(
                path,
                params,
                ErrorKind.HTTP,
                f"n8n returned HTTP {exc.code} {exc.reason}.",
                status_code=exc.code,
            )
        except (socket.timeout, TimeoutError):
            return self._fail(
                path,
                params,
                ErrorKind.TIMEOUT,
                f"n8n did not answer within {self.timeout:g}s.",
            )
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                return self._fail(
                    path,
                    params,
                    ErrorKind.TIMEOUT,
                    f"n8n did not answer within {self.timeout:g}s.",
                )
            return self._fail(
                path, params, ErrorKind.NETWORK, f"Could not reach n8n: {reason}."
            )
        except Exception as exc:  # pragma: no cover - defence in depth
            logger.warning("n8n panel: unexpected transport failure: %r", exc)
            return self._fail(
                path, params, ErrorKind.UNEXPECTED, "Unexpected error talking to n8n."
            )

        if len(raw) > MAX_RESPONSE_BYTES:
            return self._fail(
                path,
                params,
                ErrorKind.DECODE,
                "n8n response exceeded the size limit.",
            )

        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._fail(
                path, params, ErrorKind.DECODE, "n8n returned a body that is not JSON."
            )

        if not isinstance(body, dict):
            return self._fail(
                path, params, ErrorKind.DECODE, "n8n returned an unexpected body."
            )

        return body, False, _now()

    def _fail(
        self,
        path: str,
        params: dict[str, Any],
        kind: str,
        message: str,
        *,
        status_code: int | None = None,
    ) -> N8nResult:
        """Record a short-lived negative cache entry and return the failure.

        Without this, a dead n8n costs every admin page load a full timeout.
        """
        _cache_set(
            self._cache_key(path, params),
            {"__error__": {"kind": kind, "message": message, "status_code": status_code}},
            self.error_cache_ttl,
        )
        logger.info("n8n panel: %s (%s)", message, kind)
        return N8nResult.failure(kind, message, status_code=status_code)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_client() -> N8nClient:
    """Build a client from settings.

    A module-level function rather than a module-level instance so tests can
    patch it, and so a settings override takes effect without a reimport.
    """
    return N8nClient(
        base_url=(getattr(settings, "N8N_URL", "") or "").strip(),
        token=(getattr(settings, "N8N_API_TOKEN", "") or "").strip(),
        timeout=float(getattr(settings, "N8N_TIMEOUT", 5)),
        cache_ttl=int(getattr(settings, "N8N_CACHE_TTL", 60)),
        error_cache_ttl=int(getattr(settings, "N8N_CACHE_ERROR_TTL", 15)),
    )
