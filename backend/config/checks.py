"""Boot-time deployment checks (implementation spec §6).

> *"Boot-time check (a `django.core.checks` deploy check, not a comment): refuse
> to boot when `not DEBUG` and the email backend is console, or the Anymail key
> is empty. An empty mail config must never produce a green UI and zero
> delivered codes."*

These are registered as **ordinary** checks, not ``deploy=True`` ones. Deploy
checks run only under ``manage.py check --deploy``; ordinary checks additionally
run on every management command — including the ``migrate`` that
``entrypoint.sh`` executes on container start. Registering them as ordinary is
therefore what makes "refuse to boot" literally true, and ``check --deploy``
still runs them because ``--deploy`` is a superset.

Registration happens in ``apps.common.apps.CommonConfig.ready()``.
"""

from __future__ import annotations

from django.conf import settings
from django.core.checks import Error, Tags, Warning as CheckWarning, register

#: Backends that accept mail and deliver it nowhere a user can read.
NON_DELIVERING_EMAIL_BACKENDS = frozenset(
    {
        "django.core.mail.backends.console.EmailBackend",
        "django.core.mail.backends.dummy.EmailBackend",
        "django.core.mail.backends.locmem.EmailBackend",
        "django.core.mail.backends.filebased.EmailBackend",
    }
)

#: Anymail's Brevo (ex-Sendinblue) ESP backends, and the ANYMAIL key each needs.
ANYMAIL_BACKEND_KEYS = {
    "anymail.backends.brevo.EmailBackend": "BREVO_API_KEY",
    "anymail.backends.sendinblue.EmailBackend": "SENDINBLUE_API_KEY",
    "anymail.backends.mailgun.EmailBackend": "MAILGUN_API_KEY",
}

E_CONSOLE_BACKEND = "findajob.E001"
E_EMPTY_ESP_KEY = "findajob.E002"
E_NO_FROM_ADDRESS = "findajob.E003"
E_INSECURE_SECRET_KEY = "findajob.E004"
E_SMTP_INCOMPLETE = "findajob.E005"
E_LOCAL_CACHE = "findajob.E006"

#: Backends that deliver over SMTP and therefore need EMAIL_HOST* to be complete.
SMTP_EMAIL_BACKENDS = frozenset({"django.core.mail.backends.smtp.EmailBackend"})

#: Cache backends that are per-process. Every spec §5 rate-limit budget silently
#: multiplies by the gunicorn worker count on these.
NON_SHARED_CACHE_BACKENDS = (
    "django.core.cache.backends.locmem.LocMemCache",
    "django.core.cache.backends.dummy.DummyCache",
)
W_NO_WEBHOOK_SECRET = "findajob.W001"


def _exempt() -> bool:
    """True while the test runner's forced locmem backend is in play."""
    return bool(getattr(settings, "EMAIL_CHECK_EXEMPT", False))


@register(Tags.security)
def check_email_delivery(app_configs, **kwargs):
    """Refuse to run with a mail configuration that silently drops OTP codes."""
    errors = []

    if settings.DEBUG or _exempt():
        return errors

    backend = getattr(settings, "EMAIL_BACKEND", "")

    if backend in NON_DELIVERING_EMAIL_BACKENDS:
        errors.append(
            Error(
                f"EMAIL_BACKEND is {backend!r} while DEBUG is False.",
                hint=(
                    "Login is a 6-digit email OTP: a non-delivering backend means a "
                    "green UI and zero delivered codes. Set "
                    "EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend with the "
                    "Brevo SMTP relay credentials, or run "
                    "`manage.py issue_login_code --email <address>` for break-glass access."
                ),
                id=E_CONSOLE_BACKEND,
            )
        )

    if backend in SMTP_EMAIL_BACKENDS:
        host = (getattr(settings, "EMAIL_HOST", "") or "").strip()
        user = (getattr(settings, "EMAIL_HOST_USER", "") or "").strip()
        password = getattr(settings, "EMAIL_HOST_PASSWORD", "") or ""
        port = getattr(settings, "EMAIL_PORT", 0)
        use_tls = bool(getattr(settings, "EMAIL_USE_TLS", False))
        use_ssl = bool(getattr(settings, "EMAIL_USE_SSL", False))

        missing = []
        if not host or host in {"localhost", "127.0.0.1"}:
            missing.append("EMAIL_HOST")
        if not user:
            missing.append("EMAIL_HOST_USER")
        if not password:
            missing.append("EMAIL_HOST_PASSWORD")

        if missing:
            errors.append(
                Error(
                    "SMTP mail delivery is selected but "
                    f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} "
                    "empty or points at localhost.",
                    hint=(
                        "Login is a 6-digit email OTP sent synchronously. An empty SMTP "
                        "config connects to localhost inside the container, where nothing "
                        "listens — a green UI and zero delivered codes. Set EMAIL_HOST "
                        "(smtp-relay.brevo.com), EMAIL_HOST_USER and EMAIL_HOST_PASSWORD "
                        "(a Brevo SMTP key, NOT the xkeysib-… API key)."
                    ),
                    id=E_SMTP_INCOMPLETE,
                )
            )

        if port in (587, 465) and not (use_tls or use_ssl):
            errors.append(
                Error(
                    f"EMAIL_PORT is {port} but neither EMAIL_USE_TLS nor "
                    "EMAIL_USE_SSL is set.",
                    hint=(
                        "Login codes would cross the network in the clear, and Brevo "
                        "rejects unencrypted submission on 587/465. Set EMAIL_USE_TLS=True "
                        "for 587, or EMAIL_USE_SSL=True for 465."
                    ),
                    id=E_SMTP_INCOMPLETE,
                )
            )

    required_key = ANYMAIL_BACKEND_KEYS.get(backend)
    if required_key is not None:
        anymail = getattr(settings, "ANYMAIL", {}) or {}
        if not (anymail.get(required_key) or "").strip():
            errors.append(
                Error(
                    f"EMAIL_BACKEND is {backend!r} but ANYMAIL[{required_key!r}] is empty.",
                    hint=(
                        f"Set {required_key} in the environment. Anymail raises at send "
                        "time, i.e. inside the login request, which is exactly the "
                        "failure this check exists to prevent."
                    ),
                    id=E_EMPTY_ESP_KEY,
                )
            )

    if not (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").strip():
        errors.append(
            Error(
                "DEFAULT_FROM_EMAIL is empty while DEBUG is False.",
                hint="Set DEFAULT_FROM_EMAIL to a verified sender on the Brevo account.",
                id=E_NO_FROM_ADDRESS,
            )
        )

    return errors


@register(Tags.security)
def check_secret_key(app_configs, **kwargs):
    """The published development key must never reach a non-DEBUG process."""
    if settings.DEBUG:
        return []

    insecure = getattr(settings, "INSECURE_DEV_SECRET_KEY", None)
    if insecure and settings.SECRET_KEY == insecure:
        return [
            Error(
                "SECRET_KEY is still the built-in development key while DEBUG is False.",
                hint=(
                    "Generate a real key: python -c 'from django.core.management.utils "
                    "import get_random_secret_key as g; print(g())'"
                ),
                id=E_INSECURE_SECRET_KEY,
            )
        ]
    return []


@register(Tags.security, deploy=True)
def check_email_webhook_secret(app_configs, **kwargs):
    """Bounce/complaint handling needs a signed webhook to be trustworthy.

    A warning rather than an error: suppression handling degrades gracefully,
    while login does not.
    """
    if settings.DEBUG or _exempt():
        return []

    anymail = getattr(settings, "ANYMAIL", {}) or {}
    if not (anymail.get("WEBHOOK_SECRET") or "").strip():
        return [
            CheckWarning(
                "ANYMAIL['WEBHOOK_SECRET'] is empty.",
                hint=(
                    "POST /webhooks/email/brevo/ must be signature-verified before it is "
                    "allowed to write SuppressedAddress rows; without a secret the "
                    "endpoint has to reject everything."
                ),
                id=W_NO_WEBHOOK_SECRET,
            )
        ]
    return []


@register(Tags.caches)
def check_shared_cache(app_configs, **kwargs):
    """A per-process cache silently multiplies every rate limit by the worker count.

    Spec §5's budgets — the 60 s resend cooldown above all — are enforced with
    ``cache.add``. Under LocMemCache each gunicorn worker keeps its own copy, so
    a 5-worker deployment hands out 5 codes a minute instead of 1 and the
    enumeration ceiling rises by the same factor. The failure is invisible: no
    error, no log line, just limits that do not bind.
    """
    if settings.DEBUG or _exempt():
        return []

    backend = (settings.CACHES.get("default", {}) or {}).get("BACKEND", "")
    if backend in NON_SHARED_CACHE_BACKENDS:
        return [
            Error(
                f"CACHES['default'] is {backend!r} while DEBUG is False.",
                hint=(
                    "Set REDIS_URL so the rate-limit budgets are shared across gunicorn "
                    "workers. With a per-process cache the resend cooldown and the "
                    "per-email/per-IP ceilings multiply by the worker count."
                ),
                id=E_LOCAL_CACHE,
            )
        ]
    return []
