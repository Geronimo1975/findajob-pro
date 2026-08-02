"""``manage.py check_login_flow`` — the daily end-to-end login healthcheck.

    docker compose exec -T api python manage.py check_login_flow
    docker compose exec -T api python manage.py check_login_flow --json

It drives the **real** login flow over **real HTTPS** against the live site and
prints one timestamped, timed, OK/FAIL line per step. Exit status is 0 only when
every step passed.

Why it exists
-------------

Every individual piece of the login path has unit tests. What nothing tests is
the *composition* in production: Traefik's routers, nginx's proxy rules, the
CSRF cookie surviving two hops, the Brevo SMTP credential still being valid,
``seed_legal`` having actually been run on this database, the session cookie
being ``Secure``, and ``/admin/`` still being behind basicAuth. Each of those has
already broken at least once on this deployment, and every one of them fails
*silently* — the UI keeps rendering and nobody finds out until a user says "I
never got the code".

What it does and does not prove about email
-------------------------------------------

**Step 4 authenticates against the SMTP relay, then walks the envelope —
``MAIL FROM`` with the real ``DEFAULT_FROM_EMAIL`` and ``RCPT TO`` — and
``RSET``s before any DATA is transmitted.** AUTH alone was not enough: Brevo
answers AUTH happily for a credential that is not permitted to send *as*
``noreply@mail.findajob.pro``, and the refusal only appears at MAIL FROM/RCPT.
That gap is exactly the outage a real user hit on 2026-08-02 — the API answered
``202`` and no message ever arrived.

It still proves *nothing* about final deliverability: the relay accepting the
envelope does not mean the recipient's provider accepts the message, and Brevo
can log an ``Error`` at delivery time long after this command exits. Nothing is
transmitted beyond the envelope, and this command never claims a human received
anything — see the mail policy below.

Mail policy — nothing in this command may send mail to a person
---------------------------------------------------------------

* Step 4 authenticates and walks the envelope, then RSETs. No DATA is ever
  transmitted, so nothing is queued and nobody is mailed.
* Step 5 mints the login code with the **break-glass** primitive
  (``apps.accounts.services.issue_code``), the same function
  ``manage.py issue_login_code`` uses. No mailer is involved.
* Step 3 has to call ``request-code``, which *does* reach the mailer. Both
  addresses it uses are therefore put on ``SuppressedAddress`` first, so
  ``apps.accounts.mailer.send_login_code`` skips the send and still returns the
  same ``202`` (spec §6 — the suppression skip is deliberately invisible to the
  client). Without this the command would hand Brevo two guaranteed hard bounces
  every single day, because the healthcheck domain can never resolve, and a
  sustained hard-bounce rate is how a sending account gets suspended.

  The cost is stated honestly in the step-3 evidence line: step 3 verifies the
  endpoint's *shape parity*, not the SMTP leg. Step 4 is what covers SMTP.

* ``django.core.mail.send_mail`` is never called from this module.

Identity — unmistakably not a customer
--------------------------------------

Everything lives on ``@healthcheck.invalid``. ``.invalid`` is reserved by
RFC 2606 §2: it is guaranteed never to be delegated and never to resolve, so no
real person can ever hold an address there. Three identities are used:

=====================================  ==============================================
``findajob-healthcheck@``              logs in for real (steps 5–9). Never registered
                                       in advance, so step 6 exercises the *first
                                       login* path: account creation, server-resolved
                                       consent, and workspace bootstrap. Fixed,
                                       because it never touches ``request-code`` — it
                                       gets its code from break-glass — and because a
                                       successful verify resets the verify-failure
                                       budget it spends in step 8, so re-running
                                       cannot accumulate against it.
``findajob-healthcheck-known-<rnd>@``  registered via the ORM in the preflight so that
                                       step 3 compares a **registered** address
                                       against an unregistered one. Without it both
                                       sides of the parity test would be unknown
                                       addresses and the test could not fail.
``findajob-healthcheck-probe-<rnd>@``  fresh random address, never registered.
=====================================  ==============================================

Both parity addresses are **random per run**, and that is load-bearing rather
than tidy. They are the only two addresses this command puts through
``request-code``, and that endpoint carries a 60 s per-address resend cooldown
and a 5/hour per-address budget (spec §5). With a fixed "known" address, running
the command twice inside a minute would answer ``429`` for the registered side
and ``202`` for the random one — and step 3 would report an enumeration leak that
does not exist. A healthcheck whose second run cries wolf is worse than no
healthcheck.

The preflight **refuses to run** if any user on that domain carries data beyond
what this command creates (see :data:`ALLOWED_REVERSE_ACCESSORS`), and in that
case nothing is deleted.

Spec §5 rate-limit budget consumed by ONE run
---------------------------------------------

=========================================  =========  ==========================
Budget                                     Limit      Consumed per run
=========================================  =========  ==========================
Resend cooldown ``otp:cd:{email}``          60 s      1 slot on each of 2 addresses
Per email ``otp:req:{email}``               5 / h     **1** on each of 2 addresses
Per IP ``otp:ip:{ip}``                     20 / h     **2** (both request-code calls)
Global ``otp:global``                     500 / h     2
Verify failures ``otp:vf:{email}:{/24}``  10 / 15 m   1 (step 8's deliberate replay)
=========================================  =========  ==========================

That is the whole cost, and it is why step 3 makes exactly **one**
``request-code`` call per address.

**The per-IP bucket is this command's own, not a visitor's** — measured on prod
2026-08-02, not reasoned about. The request leaves the api container, goes out
to the public internet and comes back through Traefik, so it is SNATed to the
Docker bridge gateway and arrives with ``X-Forwarded-For: 172.29.0.1,
<traefik>``. ``TRUSTED_PROXY_COUNT=2`` makes ``apps.legal.request_meta.client_ip``
take ``entries[-2]``, i.e. the private ``172.29.0.1``. A real visitor's
``entries[-2]`` is whatever Traefik saw from the internet, which is never a
private address — verified in the same window, where a genuine login from
``80.187.103.151`` landed in a different bucket. So over-running this command
can only starve **itself**, never a customer.

The ceiling that follows is 20 ÷ 2 = **10 runs per hour**, and hitting it is
visible rather than silent: step 3 reports "rate limited mid-comparison … parity
is UNTESTED. This is not an enumeration leak." (measured: run 11 within six
minutes did exactly that). Daily is the intended cadence; if a burst of manual
runs trips it, the bucket is a cache key with a 3600 s TTL and heals on its own.

Where it must run
-----------------

Inside the ``findajob-api`` container, because steps 5 and 11 use the ORM while
steps 1–10 speak HTTPS to ``--base``. The two must be the same deployment, and
the command **proves** that rather than asserting it: step 5 writes the code
through the local ORM and step 6 spends it against the remote site. A code
minted in one database can never verify against another, so a mismatch surfaces
as a step-6 ``invalid_code`` failure rather than as a green run against the
wrong box.

Output
------

Default: the human report on stdout. With ``--json``: a machine-readable summary
on **stdout** (and nothing else, so it pipes into ``jq``) and the human report on
**stderr**, so a cron log keeps the narrative either way.

Failure is loud
---------------

A step that could not run — network down, container gone, an unexpected
exception — is a **FAIL**, never a skip. The command raises ``CommandError`` at
the end (exit 1) if a single step did not pass.
"""

from __future__ import annotations

import json
import smtplib
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from http.cookies import SimpleCookie
from secrets import token_hex
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.models import EmailOTP, SuppressedAddress, SuppressionReason, User
from apps.accounts.services import issue_code
from apps.tenancy.models import AuditLog, Membership, Organization

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

#: RFC 2606 §2 reserves `.invalid`: never delegated, never resolvable. No real
#: person can hold an address here, which is what makes the domain-wide sweep in
#: `_purge` safe.
SENTINEL_DOMAIN = "healthcheck.invalid"

LOGIN_ADDRESS = f"findajob-healthcheck@{SENTINEL_DOMAIN}"

#: Both parity addresses are minted fresh on every run — see the module
#: docstring. A fixed one would collide with its own 60 s resend cooldown and
#: turn a second run inside a minute into a false enumeration-leak alarm.
PARITY_KNOWN_PREFIX = "findajob-healthcheck-known-"
PROBE_PREFIX = "findajob-healthcheck-probe-"

#: `apps.tenancy.services.default_workspace_name` derives the org name from the
#: email local part and `unique_org_slug` slugifies it, so the workspace created
#: for LOGIN_ADDRESS lands on this slug (or this slug plus a numeric suffix after
#: a collision). Used only to sweep orphaned organizations left by a crashed run.
ORG_SLUG_PREFIX = "findajob-healthcheck"

#: Reverse accessors on ``User`` that this command legitimately creates. Anything
#: else with rows attached to a healthcheck user means the address has been
#: reused for something real, and the preflight refuses rather than deleting it.
ALLOWED_REVERSE_ACCESSORS = frozenset(
    {
        "memberships",      # tenancy.Membership   — CASCADE
        "consent_records",  # legal.ConsentRecord  — CASCADE
        "audit_events",     # tenancy.AuditLog     — SET_NULL, so swept by hand
    }
)

DEFAULT_BASE = "https://findajob.pro"
DEFAULT_HTTP_TIMEOUT = 15

#: SMTP needs more headroom than the 5 s login budget: step 4 does a full
#: connect + EHLO + STARTTLS + EHLO + AUTH round trip, and a slow-but-working
#: relay is not a failure worth paging anyone about.
MIN_SMTP_TIMEOUT = 10

USER_AGENT = "findajob-healthcheck/1.0 (+manage.py check_login_flow)"

PUBLIC_SURFACES = ("/", "/app.html", "/app.html?demo=1", "/auth.html")

CSRF_PATH = "/api/v1/auth/csrf/"
REQUEST_CODE_PATH = "/api/v1/auth/request-code/"
VERIFY_CODE_PATH = "/api/v1/auth/verify-code/"
LOGOUT_PATH = "/api/v1/auth/logout/"
ME_PATH = "/api/v1/me/"
ADMIN_PATH = "/admin/"
#: Deliberately NOT a real route. Traefik's admin router matches
#: ``PathPrefix(`/admin/`) || Path(`/admin`)`` precisely so that a slug like this
#: does not get swept into the basicAuth gate — a bare ``PathPrefix(`/admin`)``
#: would also match ``/administration`` and ``/admin-export``. A 401 here would
#: mean the rule had been loosened back into a raw prefix.
ADMIN_DECOY_PATH = "/adminx"

SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"

#: Machine codes that mean "the application evaluated the replayed code and
#: refused it" (spec §3). Step 8 requires one of these rather than merely a
#: non-200: a 3xx, a 502 or a 404 also fail the ``!= 200`` test while proving
#: nothing at all about single use, because the request never reached Django.
#: ``rate_limited`` is deliberately NOT here — it means the verify budget
#: rejected the request *before* looking at the code, so the property is
#: untested.
CODE_REJECTED_CODES = frozenset({"code_expired", "invalid_code", "too_many_attempts"})


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    # datetime.timezone.utc — django.utils.timezone.utc was removed in Django 5.0.
    return datetime.now(dt_timezone.utc)


def _secrets() -> tuple[str, ...]:
    """Values that must never appear in this command's output."""
    return tuple(
        str(value)
        for value in (
            getattr(settings, "EMAIL_HOST_PASSWORD", ""),
            getattr(settings, "SECRET_KEY", ""),
            (getattr(settings, "ANYMAIL", {}) or {}).get("BREVO_API_KEY", ""),
            (getattr(settings, "ANYMAIL", {}) or {}).get("WEBHOOK_SECRET", ""),
        )
        if value
    )


def scrub(text: str) -> str:
    """Redact configured secrets from a message before it is printed.

    Belt and braces: nothing here deliberately handles a secret, but step 4
    prints an SMTP server's own reply and a provider that echoed a credential
    back would otherwise put it straight into a log file.
    """
    cleaned = str(text)
    for secret in _secrets():
        if len(secret) >= 6:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned


def json_shape(value, prefix: str = "") -> list[str]:
    """Describe a JSON value's structure as a sorted list of ``path:type``.

    Used for enumeration parity: two responses must have the *same shape* while
    having different values (``masked_email`` necessarily differs). Comparing
    shape rather than keys also catches a leak encoded as a type change — say
    ``org: null`` for an unknown address and ``org: {...}`` for a known one.
    """
    if isinstance(value, dict):
        out: list[str] = []
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else str(key)
            out.extend(json_shape(value[key], child))
        return out
    if isinstance(value, list):
        # Element shapes are deduplicated, so [] and [x] differ (a real
        # structural difference) while [x] and [x, x] do not (a count, which is
        # not shape).
        out = [f"{prefix}[]:list"]
        seen: set[str] = set()
        for item in value:
            for shape in json_shape(item, f"{prefix}[]"):
                if shape not in seen:
                    seen.add(shape)
                    out.append(shape)
        return out
    return [f"{prefix}:{type(value).__name__}"]


class StepFailure(Exception):
    """A step decided it failed. Carries structured detail for ``--json``."""

    def __init__(self, message: str, **detail) -> None:
        super().__init__(message)
        self.detail = detail


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect.

    Step 1's whole point is that ``/app.html`` must answer 200 rather than
    bouncing to a login page, and a redirect-following client cannot tell the
    difference between "served" and "served after a detour". Returning ``None``
    from :meth:`redirect_request` makes urllib fall through to
    ``HTTPDefaultErrorHandler``, so a 3xx arrives as an ``HTTPError`` and is
    reported with its real status and ``Location``.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


@dataclass
class Reply:
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    elapsed_ms: int

    def header(self, name: str) -> str | None:
        wanted = name.lower()
        for key, value in self.headers:
            if key.lower() == wanted:
                return value
        return None

    def all_headers(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]

    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:  # noqa: BLE001 - a non-JSON body is data, not a crash
            return None

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")


def urllib_transport(method: str, url: str, headers: dict, body: bytes | None,
                     timeout: int) -> tuple[int, list[tuple[str, str]], bytes]:
    """The real network. Replaced wholesale by the test suite.

    Every non-2xx status — 3xx included, because redirects are suppressed above —
    arrives as ``HTTPError``, which *is* a response object (``.code``,
    ``.headers``, ``.read()``). Normalising both branches here is what lets every
    caller treat a 401 exactly like a 200.
    """
    opener = urllib.request.build_opener(_NoRedirect())
    request = urllib.request.Request(url, data=body, method=method)
    for name, value in headers.items():
        request.add_header(name, value)
    try:
        with opener.open(request, timeout=timeout) as response:
            return (
                int(response.status),
                list(response.headers.items()),
                response.read(),
            )
    except urllib.error.HTTPError as exc:
        payload = b""
        try:
            payload = exc.read()
        except Exception:  # noqa: BLE001 - the status still matters
            pass
        return int(exc.code), list(exc.headers.items()), payload


class HttpSession:
    """A browser-shaped HTTP client: cookie jar, CSRF header, ``Origin``.

    Cookies are handled explicitly rather than with :mod:`http.cookiejar`. The
    two cookies that matter — ``csrftoken`` and ``sessionid`` — are same-origin
    and path ``/``, so a jar buys nothing, while an explicit store lets step 2
    and step 6 assert on the cookie *attributes* (``Secure`` / ``HttpOnly`` /
    ``SameSite``), which is the part that silently regresses.
    """

    def __init__(self, base: str, *, timeout: int = DEFAULT_HTTP_TIMEOUT,
                 transport=urllib_transport) -> None:
        self.base = base.rstrip("/")
        parts = urlsplit(self.base)
        self.scheme = parts.scheme or "https"
        self.host = parts.netloc
        self.origin = f"{self.scheme}://{self.host}"
        self.timeout = timeout
        self.transport = transport
        #: name -> {"value": str, "attrs": {lowercased attribute: value}}
        self.cookies: dict[str, dict] = {}

    # -- cookies ---------------------------------------------------------

    def _absorb(self, reply: Reply) -> None:
        for raw in reply.all_headers("Set-Cookie"):
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except Exception:  # noqa: BLE001 - a malformed cookie is not a crash
                continue
            for name, morsel in jar.items():
                attrs = {
                    key.lower(): value
                    for key, value in morsel.items()
                    if value not in ("", False)
                }
                if morsel.value in ("", '""'):
                    # Django clears a cookie by setting it empty with a past
                    # expiry (this is how `logout` retires the session cookie).
                    self.cookies.pop(name, None)
                    continue
                self.cookies[name] = {"value": morsel.value, "attrs": attrs}

    def cookie(self, name: str) -> str | None:
        entry = self.cookies.get(name)
        return entry["value"] if entry else None

    def cookie_attrs(self, name: str) -> dict:
        entry = self.cookies.get(name)
        return dict(entry["attrs"]) if entry else {}

    def _cookie_header(self) -> str:
        return "; ".join(f"{name}={entry['value']}" for name, entry in self.cookies.items())

    # -- requests --------------------------------------------------------

    def request(self, method: str, path: str, *, json_body=None, csrf: bool = False,
                extra_headers: dict | None = None) -> Reply:
        url = self.base + path
        headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
        body: bytes | None = None
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if method != "GET":
            # Exactly what web/api.js sends (spec §8): the token echoed from the
            # cookie, plus the Origin the browser stamps. Django checks Origin
            # first on an https request; omitting it would make every POST fail
            # for a reason that has nothing to do with the login flow.
            headers["Origin"] = self.origin
            headers["Referer"] = f"{self.origin}/auth.html"
            if csrf:
                token = self.cookie("csrftoken")
                if token:
                    headers["X-CSRFToken"] = token
        cookie_header = self._cookie_header()
        if cookie_header:
            headers["Cookie"] = cookie_header
        if extra_headers:
            headers.update(extra_headers)

        start = time.perf_counter()
        status, response_headers, payload = self.transport(
            method, url, headers, body, self.timeout
        )
        elapsed = int((time.perf_counter() - start) * 1000)
        reply = Reply(
            status=int(status), headers=list(response_headers), body=payload or b"",
            elapsed_ms=elapsed,
        )
        self._absorb(reply)
        return reply

    def get(self, path: str, **kwargs) -> Reply:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, json_body=None, **kwargs) -> Reply:
        kwargs.setdefault("csrf", True)
        return self.request("POST", path, json_body=json_body, **kwargs)


# ---------------------------------------------------------------------------
# Result plumbing
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    number: int
    name: str
    ok: bool = False
    started_at: str = ""
    duration_ms: int = 0
    evidence: str = ""
    detail: dict = field(default_factory=dict)

    def line(self) -> str:
        verdict = "OK  " if self.ok else "FAIL"
        return (
            f"{self.started_at}  {verdict}  {self.number:>2}  "
            f"{self.name:<22} {self.duration_ms:>6}ms  {self.evidence}"
        )


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = (
        "Exercise the real login flow over real HTTPS against the live site and "
        "report OK/FAIL per step. Sends no email to anyone; cleans up after "
        "itself. Exit 0 only if every step passed."
    )

    #: Overridable seam. The test suite substitutes a transport that routes
    #: /api/ through django.test.Client (so the real views and the real database
    #: run) and returns canned replies for the nginx/Traefik-owned surfaces.
    transport = staticmethod(urllib_transport)

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--base",
            default=DEFAULT_BASE,
            help=f"Origin to test against (default: {DEFAULT_BASE}).",
        )
        parser.add_argument(
            "--timeout",
            type=int,
            default=DEFAULT_HTTP_TIMEOUT,
            help=f"Per-request HTTP timeout in seconds (default: {DEFAULT_HTTP_TIMEOUT}).",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            dest="as_json",
            help=(
                "Emit a machine-readable summary on stdout and the human report "
                "on stderr."
            ),
        )

    # -- lifecycle -------------------------------------------------------

    def handle(self, *args, **options) -> None:
        self.base = str(options["base"]).rstrip("/")
        self.http_timeout = max(1, int(options["timeout"]))
        self.as_json = bool(options.get("as_json"))
        self.results: list[StepResult] = []

        self.session = HttpSession(
            self.base, timeout=self.http_timeout, transport=self.transport
        )
        self.known_address = f"{PARITY_KNOWN_PREFIX}{token_hex(8)}@{SENTINEL_DOMAIN}"
        self.probe_address = f"{PROBE_PREFIX}{token_hex(8)}@{SENTINEL_DOMAIN}"

        # Carried between steps.
        self.issued_code: str | None = None
        self.issued_otp_pk: int | None = None
        self.identity: dict = {}
        self.session_key: str | None = None

        started = _now()
        self._report(f"# check_login_flow  base={self.base}  started={_iso(started)}")

        preflight_ok = self._run(0, "preflight", self._step_preflight)
        if preflight_ok:
            try:
                self._run(1, "public-surfaces", self._step_public_surfaces)
                self._run(2, "csrf-cookie", self._step_csrf)
                self._run(3, "enumeration-parity", self._step_enumeration_parity)
                self._run(4, "smtp-transport", self._step_smtp)
                self._run(5, "break-glass-code", self._step_break_glass)
                self._run(6, "verify-code", self._step_verify_code)
                self._run(7, "session-me", self._step_me)
                self._run(8, "single-use-burn", self._step_burn)
                self._run(9, "logout", self._step_logout)
                self._run(10, "admin-gates", self._step_admin_gates)
            finally:
                # Always. A failure in step 6 must not leave a user, an
                # organization and three OTP rows behind for tomorrow's run.
                self._run(11, "cleanup", self._step_cleanup)
        else:
            # "A step that could not run is a FAIL, never a skip." Cleanup is
            # deliberately NOT attempted: the only way the preflight refuses is
            # that the sentinel address carries data this command did not
            # create, and deleting that is exactly what must not happen.
            for number, name in (
                (1, "public-surfaces"), (2, "csrf-cookie"), (3, "enumeration-parity"),
                (4, "smtp-transport"), (5, "break-glass-code"), (6, "verify-code"),
                (7, "session-me"), (8, "single-use-burn"), (9, "logout"),
                (10, "admin-gates"), (11, "cleanup"),
            ):
                self._record(
                    StepResult(
                        number=number,
                        name=name,
                        ok=False,
                        started_at=_iso(_now()),
                        evidence="did not run: the preflight refused, nothing was deleted",
                        detail={"skipped_because": "preflight_failed"},
                    )
                )

        finished = _now()
        passed = sum(1 for result in self.results if result.ok)
        failed = len(self.results) - passed
        summary = {
            "command": "check_login_flow",
            "base": self.base,
            "started_at": _iso(started),
            "finished_at": _iso(finished),
            "duration_ms": int((finished - started).total_seconds() * 1000),
            "ok": failed == 0,
            "passed": passed,
            "failed": failed,
            "identities": {
                "login": LOGIN_ADDRESS,
                "parity_known": self.known_address,
                "parity_probe": self.probe_address,
            },
            "budget_consumed": {
                "request_code_calls": 2,
                "verify_code_calls": 2,
                "verify_failures_recorded": 1,
                "emails_sent_to_a_human": 0,
            },
            "steps": [
                {
                    "n": result.number,
                    "name": result.name,
                    "ok": result.ok,
                    "started_at": result.started_at,
                    "duration_ms": result.duration_ms,
                    "evidence": result.evidence,
                    "detail": result.detail,
                }
                for result in self.results
            ],
        }

        self._report(
            f"# {'PASS' if failed == 0 else 'FAIL'}  "
            f"{passed}/{len(self.results)} steps OK  "
            f"({summary['duration_ms']}ms)"
        )

        if self.as_json:
            self.stdout.write(json.dumps(summary, indent=2, sort_keys=False))

        if failed:
            raise CommandError(
                f"login healthcheck FAILED: {failed} of {len(self.results)} steps "
                f"did not pass (see the report above)",
                returncode=1,
            )

    # -- reporting -------------------------------------------------------

    def _report(self, line: str) -> None:
        """Human output. Moves to stderr under ``--json`` so stdout stays pure."""
        stream = self.stderr if self.as_json else self.stdout
        stream.write(scrub(line), style_func=lambda text: text)

    def _record(self, result: StepResult) -> None:
        self.results.append(result)
        self._report(result.line())

    def _run(self, number: int, name: str, step) -> bool:
        started = _now()
        clock = time.perf_counter()
        result = StepResult(number=number, name=name, started_at=_iso(started))
        try:
            evidence, detail = step()
            result.ok = True
            result.evidence = evidence
            result.detail = detail or {}
        except StepFailure as exc:
            result.evidence = scrub(str(exc))
            result.detail = dict(exc.detail)
        except Exception as exc:  # noqa: BLE001 — an unexpected error is a FAIL
            # Deliberately broad. A DNS failure, a dead container, a TLS error
            # and a bug in this file all mean the same thing operationally: the
            # step could not be shown to pass, so it did not pass.
            result.evidence = f"{type(exc).__name__}: {scrub(str(exc)) or '(no message)'}"
            result.detail = {"exception": type(exc).__name__}
        result.duration_ms = int((time.perf_counter() - clock) * 1000)
        self._record(result)
        return result.ok

    # -- step 0: preflight ----------------------------------------------

    def _step_preflight(self):
        """Prove the sentinel identities are ours, purge leftovers, set the stage.

        Three jobs, in order:

        1. **Refuse** if any user on the sentinel domain carries data this
           command does not create. That is the "never touch a customer" guard,
           and it fails the whole run without deleting anything.
        2. **Purge** whatever a crashed previous run left behind, so step 6
           always exercises the *first login* path (account creation + consent +
           workspace bootstrap) rather than the cheaper returning-user path.
        3. **Stage**: create the registered parity identity, and suppress both
           addresses step 3 will mail so no message leaves the box.
        """
        if not self.base.startswith(("http://", "https://")):
            raise StepFailure(f"--base must be an absolute URL, got {self.base!r}")

        blockers = self._foreign_data_blockers()
        if blockers:
            raise StepFailure(
                "REFUSING TO RUN: a sentinel address carries data this command "
                "did not create — " + "; ".join(blockers),
                blockers=blockers,
            )

        purged = self._purge()

        User.objects.create_user(email=self.known_address, lang="en", is_verified=True)
        for address in (self.known_address, self.probe_address):
            # spec §6: a suppressed address is skipped by the mailer and still
            # answered 202. This is what keeps step 3 from hard-bouncing Brevo
            # twice a day against a domain that can never resolve.
            SuppressedAddress.suppress(address, SuppressionReason.MANUAL)

        scheme_note = "" if self.base.startswith("https://") else "  (base is NOT https)"
        return (
            f"sentinel domain @{SENTINEL_DOMAIN}; purged {sum(purged.values())} "
            f"leftover rows {purged}; parity identity registered; 2 addresses "
            f"suppressed so no mail leaves the box{scheme_note}",
            {
                "purged": purged,
                "known_address": self.known_address,
                "probe_address": self.probe_address,
            },
        )

    def _foreign_data_blockers(self) -> list[str]:
        """Reasons the sentinel identities must not be touched.

        Walks ``User._meta.related_objects`` rather than naming tables, so an app
        added later that hangs data off ``User`` blocks this command *by default*
        instead of having its rows quietly deleted every night.
        """
        blockers: list[str] = []
        for user in User.objects.filter(email__endswith=f"@{SENTINEL_DOMAIN}"):
            label = user.email
            if user.is_staff or user.is_superuser:
                blockers.append(f"{label}: is_staff/is_superuser")
            if user.has_usable_password():
                blockers.append(f"{label}: has a usable password")
            try:
                if user.groups.exists():
                    blockers.append(f"{label}: belongs to auth groups")
                if user.user_permissions.exists():
                    blockers.append(f"{label}: carries user permissions")
            except Exception as exc:  # noqa: BLE001 — unknown state = refuse
                blockers.append(f"{label}: could not inspect permissions ({exc})")

            for relation in User._meta.related_objects:
                accessor = relation.get_accessor_name()
                if accessor in ALLOWED_REVERSE_ACCESSORS:
                    continue
                model = relation.related_model
                try:
                    count = model._base_manager.filter(
                        **{relation.field.name: user}
                    ).count()
                except Exception as exc:  # noqa: BLE001 — unknown state = refuse
                    blockers.append(
                        f"{label}: could not count {model._meta.label} "
                        f"via {accessor} ({exc})"
                    )
                    continue
                if count:
                    blockers.append(
                        f"{label}: {count} {model._meta.label} row(s) via {accessor}"
                    )
        return blockers

    # -- step 1: public surfaces ----------------------------------------

    def _step_public_surfaces(self):
        """``/``, ``/app.html``, ``/app.html?demo=1``, ``/auth.html`` are free.

        Three properties, all of which have a plausible way to regress:

        * **200, not 3xx.** Attaching the ``findajob-forward-auth`` middleware to
          the main router, or putting an SPA fallback back into nginx, turns
          these into redirects. Redirects are not followed here on purpose.
        * **no ``WWW-Authenticate``.** Widening the admin router's rule to a bare
          ``PathPrefix(`/`)``-ish match, or attaching ``findajob-admin-auth`` to
          the wrong router, puts basicAuth in front of the public demo.
        * ``/app.html?demo=1`` behaves exactly like ``/app.html`` — the demo path
          must never require anything.
        """
        observations = []
        problems = []
        for path in PUBLIC_SURFACES:
            reply = self.session.get(path)
            challenge = reply.header("WWW-Authenticate")
            location = reply.header("Location")
            observations.append(
                {
                    "path": path,
                    "status": reply.status,
                    "www_authenticate": challenge,
                    "location": location,
                    "ms": reply.elapsed_ms,
                }
            )
            if challenge:
                problems.append(f"{path} sent WWW-Authenticate: {challenge!r}")
            if 300 <= reply.status < 400:
                problems.append(
                    f"{path} redirected ({reply.status} -> {location or '?'})"
                )
            elif reply.status != 200:
                problems.append(f"{path} answered {reply.status}, expected 200")
        if problems:
            raise StepFailure("; ".join(problems), surfaces=observations)
        rendered = "; ".join(f"{item['path']} {item['status']}" for item in observations)
        return (
            f"{rendered}; no WWW-Authenticate on any; no redirects",
            {"surfaces": observations},
        )

    # -- step 2: CSRF ----------------------------------------------------

    def _step_csrf(self):
        """``GET /auth/csrf/`` hands the browser a usable token.

        ``web/api.js``'s ``bootstrapCsrf()`` makes exactly this call before any
        form can submit, so a regression here (``CSRF_COOKIE_HTTPONLY = True``,
        a lost ``ensure_csrf_cookie``, a proxy stripping ``Set-Cookie``) breaks
        every login while the page still renders perfectly.
        """
        reply = self.session.get(CSRF_PATH)
        if reply.status != 200:
            raise StepFailure(
                f"{CSRF_PATH} answered {reply.status}, expected 200",
                status=reply.status, body=reply.text[:400],
            )
        set_cookie = reply.all_headers("Set-Cookie")
        if not any("csrftoken=" in header for header in set_cookie):
            raise StepFailure(
                "no csrftoken in any Set-Cookie header",
                set_cookie_count=len(set_cookie),
            )
        token = self.session.cookie("csrftoken")
        if not token:
            raise StepFailure("csrftoken cookie was sent but did not parse into a value")
        attrs = self.session.cookie_attrs("csrftoken")
        if self.base.startswith("https://") and not attrs.get("secure"):
            raise StepFailure(
                "csrftoken cookie is not marked Secure over https "
                "(config.settings.prod sets CSRF_COOKIE_SECURE = True)",
                attrs=attrs,
            )
        if attrs.get("httponly"):
            # web/api.js has to read this cookie from JavaScript to build the
            # X-CSRFToken header (spec §8). HttpOnly here breaks every POST.
            raise StepFailure(
                "csrftoken cookie is HttpOnly; web/api.js cannot read it",
                attrs=attrs,
            )
        return (
            f"200; csrftoken set ({len(token)} chars), "
            f"samesite={attrs.get('samesite', '-')} secure={bool(attrs.get('secure'))} "
            f"httponly=False",
            {"token_length": len(token), "attrs": attrs},
        )

    # -- step 3: enumeration parity -------------------------------------

    def _step_enumeration_parity(self):
        """A registered address and an unregistered one must be indistinguishable.

        This is the property that stops account enumeration (spec §6), and it is
        the reason ``request-code`` never queries ``User`` and ``EmailOTP`` has
        no FK to it. A difference of **any** kind — status, key set, or value
        type — is a FAIL.

        Scope, stated honestly: both addresses are on the suppression list, so
        this exercises the endpoint's shape parity and *not* the SMTP leg
        (step 4 covers that) and *not* timing parity (which a single sample over
        the public internet cannot measure). One call per address, which is the
        entire spec §5 budget this command spends on ``request-code``.
        """
        known = self.session.post(
            REQUEST_CODE_PATH, {"email": self.known_address, "lang": "en"}
        )
        unknown = self.session.post(
            REQUEST_CODE_PATH, {"email": self.probe_address, "lang": "en"}
        )

        known_shape = json_shape(known.json())
        unknown_shape = json_shape(unknown.json())
        detail = {
            "known": {"status": known.status, "shape": known_shape, "ms": known.elapsed_ms},
            "unknown": {
                "status": unknown.status, "shape": unknown_shape, "ms": unknown.elapsed_ms
            },
        }

        # Diagnosis before verdict. A 429 on either side is a *shared* budget
        # running out between the two calls — the per-IP (20/h) or global (500/h)
        # bucket — not a difference the endpoint drew from the addresses. Both
        # parity addresses are minted fresh each run precisely so the per-address
        # budgets can never do this; calling it a leak would be a false alarm on
        # the one signal that must never cry wolf.
        throttled = [
            label for label, reply in (("registered", known), ("unregistered", unknown))
            if reply.status == 429
        ]
        if throttled:
            raise StepFailure(
                f"rate limited mid-comparison ({', '.join(throttled)} answered 429) — "
                f"a shared per-IP or global budget is exhausted, so parity is "
                f"UNTESTED. This is not an enumeration leak.",
                **detail,
            )

        if known.status != unknown.status:
            raise StepFailure(
                f"ENUMERATION LEAK: registered address answered {known.status}, "
                f"unregistered answered {unknown.status}",
                **detail,
            )
        if known_shape != unknown_shape:
            raise StepFailure(
                f"ENUMERATION LEAK: response shapes differ — "
                f"registered {known_shape} vs unregistered {unknown_shape}",
                **detail,
            )
        if known.status != 202:
            # Same status on both sides but not the pinned 202: parity holds
            # vacuously and proves nothing. Most likely the per-IP or global
            # budget is exhausted, or delivery is down.
            body = known.json() or {}
            code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
            raise StepFailure(
                f"both addresses answered {known.status} (error code {code!r}), "
                f"expected 202 — parity is untested at this status",
                **detail,
            )
        return (
            f"both 202, identical shape {known_shape}; "
            f"registered={known.elapsed_ms}ms unregistered={unknown.elapsed_ms}ms; "
            f"both suppressed, so no message left the box (SMTP is step 4)",
            detail,
        )

    # -- step 4: SMTP ----------------------------------------------------

    def _step_smtp(self):
        """Connect, STARTTLS, AUTH — then QUIT without sending anything.

        This is the one check that can see the Brevo SMTP key expiring
        (2027-08-01, and nothing else warns), a rotated credential that never
        made it into ``/root/findajob/.env``, or an ``EMAIL_BACKEND`` that
        drifted back to the console backend.

        What it does **not** prove: that a message would be delivered. Brevo
        accepts a message from an unauthenticated sending domain and only then
        logs a delivery ``Error`` — so a clean AUTH here plus a clean 202 from
        our own API still means nothing arrived. Deliverability is confirmed in
        Brevo's transactional log, not from this box.
        """
        envelope_ok = False
        backend = getattr(settings, "EMAIL_BACKEND", "")
        if backend != SMTP_BACKEND:
            raise StepFailure(
                f"EMAIL_BACKEND is {backend!r}, not {SMTP_BACKEND!r} — the login "
                f"path cannot deliver anything",
                backend=backend,
            )
        host = getattr(settings, "EMAIL_HOST", "")
        port = int(getattr(settings, "EMAIL_PORT", 0) or 0)
        user = getattr(settings, "EMAIL_HOST_USER", "")
        password = getattr(settings, "EMAIL_HOST_PASSWORD", "")
        use_tls = bool(getattr(settings, "EMAIL_USE_TLS", False))
        use_ssl = bool(getattr(settings, "EMAIL_USE_SSL", False))
        if not host or not port:
            raise StepFailure(f"EMAIL_HOST/EMAIL_PORT are not configured ({host!r}:{port})")
        if not user or not password:
            raise StepFailure(
                "EMAIL_HOST_USER / EMAIL_HOST_PASSWORD are empty — SMTP AUTH "
                "cannot be attempted",
                has_user=bool(user), has_password=bool(password),
            )

        timeout = max(int(getattr(settings, "EMAIL_TIMEOUT", 5) or 5), MIN_SMTP_TIMEOUT)
        connection = None
        try:
            if use_ssl:
                connection = smtplib.SMTP_SSL(
                    host, port, timeout=timeout, context=ssl.create_default_context()
                )
                mode = "implicit TLS"
            else:
                connection = smtplib.SMTP(host, port, timeout=timeout)
                mode = "plain"
            banner = getattr(connection, "_greeting", "") or ""
            connection.ehlo()
            if use_tls and not use_ssl:
                connection.starttls(context=ssl.create_default_context())
                connection.ehlo()
                mode = "STARTTLS"
            connection.login(user, password)

            # The envelope, which AUTH alone does not exercise. This is where an
            # unverified sending domain is rejected — Brevo answers AUTH happily
            # for a credential that is not allowed to send AS
            # noreply@mail.findajob.pro, and the refusal lands at MAIL FROM or
            # RCPT TO. Without this the check is blind to precisely the outage
            # that made a real user receive nothing while the API said 202.
            #
            # RSET immediately after: the transaction is abandoned, no DATA is
            # ever sent, and nothing is queued or delivered to anybody.
            # The BARE addr-spec, not the formatted header. MAIL FROM takes an
            # envelope sender; a display name in there is a syntax error.
            # `sanitize_address` would be the Django way but it is deprecated in
            # 6.0 (RemovedInDjango70Warning) and returns the formatted form.
            from email.utils import parseaddr

            envelope_from = parseaddr(
                getattr(settings, "DEFAULT_FROM_EMAIL", "")
            )[1]
            if not envelope_from:
                raise StepFailure(
                    "DEFAULT_FROM_EMAIL does not parse to an address: "
                    f"{getattr(settings, 'DEFAULT_FROM_EMAIL', '')!r}"
                )
            mail_code, mail_msg = connection.mail(envelope_from)
            if mail_code != 250:
                raise StepFailure(
                    f"MAIL FROM <{envelope_from}> was rejected: "
                    f"{mail_code} {scrub(mail_msg.decode('utf-8', 'replace'))} — "
                    f"the relay will not accept mail from this sender, so login "
                    f"codes cannot be delivered no matter what the API answers",
                    host=host, sender=envelope_from, smtp_code=mail_code,
                )
            rcpt_code, rcpt_msg = connection.rcpt(LOGIN_ADDRESS)
            # 250/251 accept; 550-ish rejects. The recipient is an
            # @healthcheck.invalid sentinel, so a refusal here is about the
            # SENDER's standing with the relay, not about that mailbox.
            if rcpt_code not in (250, 251):
                raise StepFailure(
                    f"RCPT TO was rejected: {rcpt_code} "
                    f"{scrub(rcpt_msg.decode('utf-8', 'replace'))} — the relay "
                    f"accepted AUTH but refuses to route mail from "
                    f"<{envelope_from}>",
                    host=host, sender=envelope_from, smtp_code=rcpt_code,
                )
            connection.rset()
            envelope_ok = True
        except (smtplib.SMTPException, ssl.SSLError, OSError, socket.timeout) as exc:
            raise StepFailure(
                f"SMTP {type(exc).__name__} against {host}:{port} as {user}: "
                f"{scrub(str(exc))}",
                host=host, port=port, identity=user, error=type(exc).__name__,
            ) from exc
        finally:
            if connection is not None:
                try:
                    # QUIT, never send. Nothing was transmitted beyond the
                    # handshake and AUTH.
                    connection.quit()
                except Exception:  # noqa: BLE001 - the connection is going away
                    try:
                        connection.close()
                    except Exception:  # noqa: BLE001
                        pass

        return (
            f"{host}:{port} {mode}; AUTH accepted for identity {user!r}; "
            f"MAIL FROM + RCPT TO accepted then RSET — this proves the "
            f"credential, the transport AND that the relay will route mail "
            f"from this sender. It does NOT prove a human received anything: "
            f"the message body was never transmitted and delivery is only "
            f"visible in Brevo's transactional log",
            {
                "host": host, "port": port, "mode": mode, "identity": user,
                "banner": scrub(str(banner))[:120],
                "proves": "credential+transport+envelope(sender accepted)",
                "envelope_verified": envelope_ok,
                "does_not_prove": "delivery (check Brevo's transactional log for Delivered)",
            },
        )

    # -- step 5: break-glass ---------------------------------------------

    def _step_break_glass(self):
        """Mint a real code through the same primitive ``issue_login_code`` uses.

        ``apps.accounts.services.issue_code`` — imported, never shelled out to,
        so the command fails at import time if the break-glass path is ever
        removed or renamed rather than silently falling back to something else.

        This is also the step that ties the two halves of the healthcheck
        together: the row is written through *this process's* ORM and spent in
        step 6 against ``--base``. If those are different deployments, step 6
        fails with ``invalid_code``.
        """
        otp, code = issue_code(email=LOGIN_ADDRESS, ip=None)
        self.issued_code = code
        self.issued_otp_pk = otp.pk
        live = EmailOTP.objects.live(email=LOGIN_ADDRESS).count()
        return (
            f"EmailOTP #{otp.pk} written for {LOGIN_ADDRESS} via "
            f"apps.accounts.services.issue_code; expires {_iso(otp.expires_at)}; "
            f"{live} live row(s); code not printed",
            {"otp_pk": otp.pk, "expires_at": _iso(otp.expires_at), "live_rows": live},
        )

    # -- step 6: verify ---------------------------------------------------

    def _step_verify_code(self):
        """Spend the code and assert the response matches spec §3's ``/me/`` shape.

        ``accepts_terms`` is true because this is a first-ever login: the account
        does not exist yet (the preflight guarantees it), so the run also
        exercises server-resolved consent. A ``503 service_unavailable`` here
        means ``manage.py seed_legal`` has never been run on this database —
        which would mean **no new customer can register**, and nothing else
        would tell you.
        """
        if not self.issued_code:
            raise StepFailure("no code was issued in step 5")
        reply = self.session.post(
            VERIFY_CODE_PATH,
            {
                "email": LOGIN_ADDRESS,
                "code": self.issued_code,
                "accepts_terms": True,
                "lang": "en",
            },
        )
        payload = reply.json()
        if reply.status != 200:
            error = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
            raise StepFailure(
                f"verify-code answered {reply.status} "
                f"(code={error.get('code')!r} detail={error.get('detail')!r})",
                status=reply.status, error=error,
            )
        if not isinstance(payload, dict):
            raise StepFailure("verify-code returned a non-object body", body=reply.text[:400])

        problems = self._me_contract_problems(payload)
        if payload.get("created") is not True:
            problems.append(
                "created is not True — the account already existed, so the "
                "first-login path (consent + workspace bootstrap) was not exercised"
            )
        if problems:
            raise StepFailure("; ".join(problems), payload_keys=sorted(payload))

        session_cookie = self.session.cookie("sessionid")
        if not session_cookie:
            raise StepFailure("no sessionid cookie was set by a 200 verify-code")
        attrs = self.session.cookie_attrs("sessionid")
        if not attrs.get("httponly"):
            raise StepFailure("sessionid cookie is not HttpOnly", attrs=attrs)
        if self.base.startswith("https://") and not attrs.get("secure"):
            raise StepFailure("sessionid cookie is not Secure over https", attrs=attrs)
        self.session_key = session_cookie

        user = payload["user"]
        org = payload["org"]
        self.identity = {
            "user_id": user["id"],
            "email": user["email"],
            "org_id": org["id"],
            "membership_role": payload["membership_role"],
        }
        return (
            f"200; user #{user['id']} {user['email']} is_verified={user['is_verified']}; "
            f"org #{org['id']} {org['slug']!r}; membership_role="
            f"{payload['membership_role']!r}; {len(payload['memberships'])} membership(s); "
            f"created=True; sessionid HttpOnly"
            f"{' Secure' if attrs.get('secure') else ''}",
            {"identity": self.identity, "cookie_attrs": attrs},
        )

    def _me_contract_problems(self, payload: dict) -> list[str]:
        """Spec §3's session payload, asserted key by key.

        ``verify-code`` and ``GET /api/v1/me/`` return the same shape on purpose
        so ``web/auth.html`` can redirect straight into the app; checking it in
        one place keeps the two honest about that.
        """
        problems: list[str] = []
        for key in ("user", "org", "membership_role", "memberships", "lang"):
            if key not in payload:
                problems.append(f"missing top-level key {key!r}")
        user = payload.get("user")
        if not isinstance(user, dict):
            problems.append("user is not an object")
        else:
            for key in ("id", "email", "is_verified", "lang"):
                if key not in user:
                    problems.append(f"missing user.{key}")
            if user.get("email") != LOGIN_ADDRESS:
                problems.append(
                    f"user.email is {user.get('email')!r}, expected {LOGIN_ADDRESS!r}"
                )
            if user.get("is_verified") is not True:
                problems.append("user.is_verified is not True after a successful verify")
        org = payload.get("org")
        if not isinstance(org, dict):
            problems.append("org is null or not an object — bootstrap_workspace did not run")
        else:
            for key in ("id", "name", "slug"):
                if key not in org:
                    problems.append(f"missing org.{key}")
        if payload.get("membership_role") != "owner":
            problems.append(
                f"membership_role is {payload.get('membership_role')!r}, expected 'owner'"
            )
        memberships = payload.get("memberships")
        if not isinstance(memberships, list) or not memberships:
            problems.append("memberships is empty or not a list")
        else:
            for key in ("id", "role", "org"):
                if key not in memberships[0]:
                    problems.append(f"missing memberships[0].{key}")
        return problems

    # -- step 7: /me/ ------------------------------------------------------

    def _step_me(self):
        """The session cookie alone identifies the same person.

        Separate from step 6 because they can diverge: verify-code builds its
        payload in ``apps.accounts.views`` while ``/api/v1/me/`` builds it in
        ``apps.tenancy.views``, and only a round trip proves the cookie the
        browser was handed actually authenticates.
        """
        reply = self.session.get(ME_PATH)
        if reply.status != 200:
            raise StepFailure(
                f"GET {ME_PATH} answered {reply.status} with a live session cookie",
                status=reply.status, body=reply.text[:400],
            )
        payload = reply.json()
        if not isinstance(payload, dict):
            raise StepFailure("/me/ returned a non-object body", body=reply.text[:400])
        problems = self._me_contract_problems(payload)
        user = payload.get("user") or {}
        org = payload.get("org") or {}
        if user.get("id") != self.identity.get("user_id"):
            problems.append(
                f"user.id is {user.get('id')!r}, verify-code said "
                f"{self.identity.get('user_id')!r}"
            )
        if org.get("id") != self.identity.get("org_id"):
            problems.append(
                f"org.id is {org.get('id')!r}, verify-code said "
                f"{self.identity.get('org_id')!r}"
            )
        if problems:
            raise StepFailure("; ".join(problems), payload_keys=sorted(payload))
        return (
            f"200; same identity: user #{user['id']} {user['email']}, org #{org['id']}, "
            f"role {payload['membership_role']!r}",
            {"user_id": user["id"], "org_id": org["id"]},
        )

    # -- step 8: single-use burn -------------------------------------------

    def _step_burn(self):
        """The same code, a second time, must not work.

        Single use is the property that makes a 10^6 keyspace survivable: a code
        observed once (a shared screen, a forwarded email, a mail scanner that
        pre-fetches) must be worthless. A 200 here is the most serious finding
        this command can produce.

        "Not 200" is deliberately **not** good enough. A 308 from the wrong
        hostname, a 502 from a dead container and a 404 from a moved route all
        satisfy ``!= 200`` while proving nothing — the request never reached
        Django, so the replay was never evaluated. This step therefore demands a
        4xx carrying one of :data:`CODE_REJECTED_CODES`, which only the
        application itself can produce. (Measured: pointed at the www hostname,
        the earlier ``!= 200`` version reported OK on a 308.)
        """
        if not self.issued_code:
            raise StepFailure("no code was issued in step 5")
        reply = self.session.post(
            VERIFY_CODE_PATH,
            {
                "email": LOGIN_ADDRESS,
                "code": self.issued_code,
                "accepts_terms": True,
                "lang": "en",
            },
        )
        payload = reply.json() if reply.body else None
        error = (payload or {}).get("error", {}) if isinstance(payload, dict) else {}
        code = error.get("code")
        detail = {"status": reply.status, "error_code": code}

        if reply.status == 200:
            raise StepFailure(
                "SINGLE-USE BROKEN: replaying a consumed code returned 200", **detail
            )
        if not 400 <= reply.status < 500 or code not in CODE_REJECTED_CODES:
            raise StepFailure(
                f"the replay was NOT evaluated by the application: {reply.status} "
                f"with error code {code!r}. Single use is UNTESTED — expected a 4xx "
                f"carrying one of {sorted(CODE_REJECTED_CODES)}",
                **detail,
            )
        return (
            f"replay rejected by the application: {reply.status} {code!r} — "
            f"the code was consumed",
            detail,
        )

    # -- step 9: logout -----------------------------------------------------

    def _step_logout(self):
        """Logout, then prove the session is genuinely dead.

        The second half is the part that matters. ``logout`` returning 204 while
        the session row survives is a real failure mode (a flush that did not
        happen, a cached response), and only a follow-up ``/me/`` can see it.
        401 specifically, not 403: ``web/app.html`` redirects to ``/auth.html``
        on 401 and would otherwise sit on an empty dashboard (spec §7).
        """
        logout = self.session.post(LOGOUT_PATH, None)
        if not 200 <= logout.status < 300:
            raise StepFailure(
                f"POST {LOGOUT_PATH} answered {logout.status}, expected 2xx",
                status=logout.status, body=logout.text[:400],
            )
        # Re-present the session key EXPLICITLY. Without this the assertion is a
        # tautology: logout clears the cookie client-side, the cookie jar drops
        # it, and the follow-up request carries no session at all — so a 401
        # proves only that an anonymous request is anonymous. A server that
        # answered 204 and cleared the cookie while leaving the session row
        # alive would pass. Measured: that exact stub passed the old assertion.
        replayed = self.session.get(
            ME_PATH, extra_headers={"Cookie": f"sessionid={self.session_key}"}
        ) if self.session_key else self.session.get(ME_PATH)
        if replayed.status != 401:
            raise StepFailure(
                f"GET {ME_PATH} answered {replayed.status} when the pre-logout "
                f"session cookie was presented again, expected 401 — logout did "
                f"not destroy the session server-side",
                logout_status=logout.status, me_status=replayed.status,
                body=replayed.text[:400],
            )

        # Belt and braces, and cheaper than the round trip: the row itself must
        # be gone. This catches a flush that silently did not happen.
        row_survived = None
        if self.session_key:
            from django.contrib.sessions.models import Session

            row_survived = Session.objects.filter(
                session_key=self.session_key
            ).exists()
            if row_survived:
                raise StepFailure(
                    "the django_session row for the logged-out session still "
                    "exists — logout cleared the cookie but not the server state",
                    session_key_prefix=self.session_key[:8],
                )
        return (
            f"logout {logout.status}; replaying the old session cookie still "
            f"gives 401 and the django_session row is gone — the session is dead",
            {
                "logout_status": logout.status,
                "me_status": replayed.status,
                "session_row_deleted": row_survived is False,
            },
        )

    # -- step 10: admin gates ----------------------------------------------

    def _step_admin_gates(self):
        """Gate 1 (Traefik basicAuth) is up, and its rule is not a loose prefix.

        Two assertions, and the second is the interesting one. In Traefik 2.x
        ``PathPrefix`` is a raw string prefix, so a rule written as
        ``PathPrefix(`/admin`)`` would also cover ``/administration`` and
        ``/admin-export``. ``/adminx`` answering 401 is the signature of exactly
        that mistake; today it must 404.
        """
        admin = self.session.get(ADMIN_PATH)
        challenge = admin.header("WWW-Authenticate")
        decoy = self.session.get(ADMIN_DECOY_PATH)

        problems = []
        if admin.status != 401:
            problems.append(
                f"{ADMIN_PATH} answered {admin.status}, expected 401 from Traefik basicAuth"
            )
        if not challenge:
            problems.append(f"{ADMIN_PATH} sent no WWW-Authenticate header")
        if decoy.status == 401:
            problems.append(
                f"{ADMIN_DECOY_PATH} answered 401 — the Traefik admin rule has been "
                f"loosened into a raw prefix match"
            )
        elif 300 <= decoy.status < 400:
            # A redirect is not an answer. It means the request was bounced
            # before anything decided whether the path is gated, so "not 401" is
            # an observation we did not actually make.
            problems.append(
                f"{ADMIN_DECOY_PATH} redirected ({decoy.status}) — inconclusive, the "
                f"prefix rule is UNTESTED"
            )
        if problems:
            raise StepFailure(
                "; ".join(problems),
                admin_status=admin.status, admin_challenge=challenge,
                decoy_status=decoy.status,
            )
        return (
            f"{ADMIN_PATH} 401 with WWW-Authenticate: {challenge!r}; "
            f"{ADMIN_DECOY_PATH} {decoy.status} (not 401)",
            {
                "admin_status": admin.status,
                "admin_challenge": challenge,
                "decoy_status": decoy.status,
            },
        )

    # -- step 11: cleanup ---------------------------------------------------

    def _step_cleanup(self):
        """Delete everything this run created, then prove it is gone.

        Runs in a ``finally``, so a failure anywhere above still leaves the
        database exactly as it was found. The assertion afterwards is what makes
        "safe to run every day forever" a property rather than an intention: if
        a future model starts hanging rows off the healthcheck user, the count
        will not fall to zero and this step fails.
        """
        deleted = self._purge()
        remaining = self._sentinel_footprint()
        leftovers = {name: count for name, count in remaining.items() if count}
        if leftovers:
            raise StepFailure(
                f"cleanup left rows behind: {leftovers}",
                deleted=deleted, remaining=remaining,
            )
        return (
            f"deleted {sum(deleted.values())} row(s) {deleted}; "
            f"0 rows remain for @{SENTINEL_DOMAIN}",
            {"deleted": deleted, "remaining": remaining},
        )

    # -- shared cleanup ------------------------------------------------------

    def _consent_model(self):
        """``apps.legal.ConsentRecord``, or ``None`` if legal is not installed.

        Guarded the same way ``apps.accounts.services._record_consent`` guards
        it: legal is a separate work stream, and a half-built one must not stop
        the healthcheck from cleaning up after itself.
        """
        try:
            from apps.legal.models import ConsentRecord  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - absent app, not an error here
            return None
        return ConsentRecord

    def _sentinel_users(self):
        return list(User.objects.filter(email__endswith=f"@{SENTINEL_DOMAIN}"))

    def _sentinel_footprint(self) -> dict:
        """Every row anywhere that belongs to a sentinel identity."""
        users = self._sentinel_users()
        counts = {
            "users": len(users),
            "otps": EmailOTP.objects.filter(
                email__endswith=f"@{SENTINEL_DOMAIN}"
            ).count(),
            "suppressed": SuppressedAddress.objects.filter(
                email__endswith=f"@{SENTINEL_DOMAIN}"
            ).count(),
            "memberships": Membership.all_objects.filter(user__in=users).count()
            if users
            else 0,
            "organizations": Organization.objects.filter(
                slug__startswith=ORG_SLUG_PREFIX
            ).count(),
            "audit_logs": AuditLog.all_objects.filter(actor__in=users).count()
            if users
            else 0,
            "sessions": self._session_rows(),
        }
        consent_model = self._consent_model()
        counts["consents"] = (
            consent_model.objects.filter(user__in=users).count()
            if consent_model is not None and users
            else 0
        )
        return counts

    def _session_rows(self) -> int:
        if not self.session_key:
            return 0
        try:
            from django.contrib.sessions.models import Session  # noqa: PLC0415
        except Exception:  # noqa: BLE001 - a non-db session engine
            return 0
        return Session.objects.filter(session_key=self.session_key).count()

    def _purge(self) -> dict:
        """Delete every sentinel row, in FK-safe order. Idempotent.

        Organizations need explicit handling: nothing cascades to them, because
        ``Organization`` has no foreign key to ``User``. They are found through
        the memberships first (precise) and then, for orphans a crashed run left
        with no members at all, through the slug prefix (bounded). An
        organization that has a member who is *not* a sentinel user is never
        deleted — it is reported as a blocker instead.
        """
        deleted = {
            "users": 0, "otps": 0, "suppressed": 0, "memberships": 0,
            "organizations": 0, "audit_logs": 0, "consents": 0, "sessions": 0,
        }
        users = self._sentinel_users()

        # Sessions first: the key is only known while the cookie is in hand, and
        # a flushed session row is otherwise unattributable to a user.
        if self.session_key:
            try:
                from django.contrib.sessions.models import Session  # noqa: PLC0415

                deleted["sessions"] = Session.objects.filter(
                    session_key=self.session_key
                ).delete()[0]
            except Exception:  # noqa: BLE001 - non-db session engine
                pass

        if users:
            org_ids = set(
                Membership.all_objects.filter(user__in=users).values_list(
                    "org_id", flat=True
                )
            )
            outsiders = set(
                Membership.all_objects.filter(org_id__in=org_ids)
                .exclude(user__in=users)
                .values_list("org_id", flat=True)
            )
            if outsiders:
                raise StepFailure(
                    f"organizations {sorted(outsiders)} have members that are not "
                    f"healthcheck identities; refusing to delete them",
                    organization_ids=sorted(outsiders),
                )

            consent_model = self._consent_model()
            if consent_model is not None:
                deleted["consents"] = consent_model.objects.filter(
                    user__in=users
                ).delete()[0]
            # SET_NULL, so these survive the user delete as orphans and would
            # accumulate one per run forever. Nothing writes them today; the
            # sweep exists so that the day something does, this stays true.
            deleted["audit_logs"] = AuditLog.all_objects.filter(actor__in=users).delete()[0]
            deleted["memberships"] = Membership.all_objects.filter(
                user__in=users
            ).delete()[0]
            deleted["users"] = User.objects.filter(
                pk__in=[user.pk for user in users]
            ).delete()[0]
            if org_ids:
                deleted["organizations"] += Organization.objects.filter(
                    pk__in=org_ids
                ).delete()[0]

        # Orphans from a run that died between creating the org and creating the
        # membership. Bounded by the slug prefix AND by having no members at all,
        # so a real workspace can never match both.
        orphans = Organization.objects.filter(slug__startswith=ORG_SLUG_PREFIX).exclude(
            pk__in=Membership.all_objects.values_list("org_id", flat=True)
        )
        deleted["organizations"] += orphans.delete()[0]

        deleted["otps"] = EmailOTP.objects.filter(
            email__endswith=f"@{SENTINEL_DOMAIN}"
        ).delete()[0]
        deleted["suppressed"] = SuppressedAddress.objects.filter(
            email__endswith=f"@{SENTINEL_DOMAIN}"
        ).delete()[0]
        return deleted
