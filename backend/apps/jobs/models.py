"""Jobs models — Platform catalog, scraped listings, per-member applications.

The contract is ``prototype/api.js`` (architecture rule: its mock shapes mirror
the intended DRF serializers 1:1), with the audited divergences fixed here
rather than reproduced:

- ``psd`` → ``platform_specific_data``; ``rate_limit`` (display string) →
  ``rate_limit_config`` (structured JSON).
- ``status`` lives on :class:`Application`, never on :class:`JobListing` — a
  scraped job is shared; an application is per-owner. The mock's flattened
  job rows (``status``/``sent_manually`` on a job) are a serializer concern.
- ``JobListing`` gains ``description``, ``requirements`` and ``location``,
  all rendered by the UI and required by the AI prompt spec.

Scoping: ``Platform`` and ``JobListing`` are global (the scraped catalog,
shared across tenants). ``Application`` and ``Document`` are org-scoped via
``OrgScopedManager``. Per-org platform enable/disable is a later through
model; MVP keeps the global ``is_active`` switch.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.tenancy.managers import OrgScopedManager


class ConnectorType(models.TextChoices):
    API = "api", _("API")
    RSS = "rss", _("RSS")
    SCRAPER_HTML = "scraper_html", _("HTML scraper")
    BROWSER_AUTOMATION = "browser_automation", _("browser automation")


class AuthMethod(models.TextChoices):
    NONE = "none", _("none")
    API_KEY = "api_key", _("API key")
    COOKIE_SESSION = "cookie_session", _("cookie session")
    LOGIN_FORM = "login_form", _("login form")


class ApplyMechanism(models.TextChoices):
    EMAIL = "email", _("email")
    EXTERNAL_REDIRECT = "external_redirect", _("external redirect")
    IN_PLATFORM_FORM = "in_platform_form", _("in-platform form")
    MESSAGE_TO_FOUNDER = "message_to_founder", _("message to founder")


class Risk(models.TextChoices):
    LOW = "low", _("low")
    MEDIUM = "medium", _("medium")
    HIGH = "high", _("high")


class Platform(models.Model):
    """One source-platform module (the adapter catalog row).

    The per-platform risk rules are law: ``auto_send`` may be True only where
    ``apply_mechanism`` is email and ``risk`` is low. Guarded by a
    ``CheckConstraint`` so not even the admin can mis-set it.
    """

    #: Stable slug used by the frontend and the adapter registry ('remoteok').
    slug = models.SlugField(_("slug"), primary_key=True, max_length=32)
    name = models.CharField(_("name"), max_length=100)
    connector_type = models.CharField(
        _("connector type"), max_length=32, choices=ConnectorType.choices
    )
    auth_method = models.CharField(
        _("auth method"), max_length=32, choices=AuthMethod.choices
    )
    apply_mechanism = models.CharField(
        _("apply mechanism"), max_length=32, choices=ApplyMechanism.choices
    )
    risk = models.CharField(_("risk"), max_length=8, choices=Risk.choices)
    auto_send = models.BooleanField(_("auto-send"), default=False)
    is_active = models.BooleanField(_("active"), default=True)
    #: Structured limits, e.g. ``{"daily": 60, "delay_seconds": 5}``.
    rate_limit_config = models.JSONField(_("rate limit config"), default=dict)
    note = models.TextField(_("note"), blank=True)

    class Meta:
        verbose_name = _("platform")
        verbose_name_plural = _("platforms")
        ordering = ("slug",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(auto_send=False)
                    | models.Q(apply_mechanism="email", risk="low")
                ),
                name="auto_send_only_low_risk_email",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - admin nicety
        return self.name


class JobListing(models.Model):
    """A scraped/ingested job. Global: shared by every tenant.

    Anything platform-specific (``apply_email``, ``founder``, ``easy_apply``)
    lives in ``platform_specific_data``. Adapters extract ``apply_email``
    best-effort at ingest (feeds do not reliably carry it); the hard check is
    at send time, where a missing address on an email-mechanism platform is a
    422 ``missing_apply_email``.
    """

    platform = models.ForeignKey(
        Platform,
        on_delete=models.PROTECT,
        related_name="listings",
        verbose_name=_("platform"),
    )
    title = models.CharField(_("title"), max_length=255)
    company = models.CharField(_("company"), max_length=255)
    url = models.URLField(_("url"), blank=True)
    location = models.CharField(_("location"), max_length=255, blank=True)
    salary = models.CharField(_("salary"), max_length=100, blank=True)
    description = models.TextField(_("description"), blank=True)
    requirements = models.TextField(_("requirements"), blank=True)
    tags = models.JSONField(_("tags"), default=list)
    platform_specific_data = models.JSONField(
        _("platform-specific data"), default=dict
    )
    scraped_at = models.DateTimeField(_("scraped at"))
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)

    class Meta:
        verbose_name = _("job listing")
        verbose_name_plural = _("job listings")
        ordering = ("-scraped_at", "-id")
        constraints = [
            # One row per source posting; re-scrapes update in place.
            models.UniqueConstraint(
                fields=["platform", "url"],
                name="one_listing_per_platform_url",
                condition=~models.Q(url=""),
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - admin nicety
        return f"{self.title} @ {self.company}"

    @property
    def apply_email(self) -> str | None:
        value = (self.platform_specific_data or {}).get("apply_email")
        return value or None


class DocumentKind(models.TextChoices):
    CV = "cv", _("CV")
    COVER_LETTER = "cover_letter", _("cover letter")
    CERTIFICATE = "certificate", _("certificate")
    DIPLOMA = "diploma", _("diploma")
    OTHER = "other", _("other")


def document_upload_to(instance: "Document", filename: str) -> str:
    # Partition by org so a future per-org export/erasure is a prefix delete.
    return f"documents/org-{instance.org_id}/{filename}"


class Document(models.Model):
    """A member's document (CV, cover letter, certificate…). Org-scoped."""

    org = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name=_("organization"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="documents",
        verbose_name=_("user"),
    )
    name = models.CharField(_("name"), max_length=255)
    kind = models.CharField(
        _("kind"), max_length=16, choices=DocumentKind.choices,
        default=DocumentKind.OTHER,
    )
    tags = models.JSONField(_("tags"), default=list)
    file = models.FileField(_("file"), upload_to=document_upload_to)
    size_bytes = models.PositiveBigIntegerField(_("size (bytes)"), default=0)
    #: Computed server-side when scoring lands; never invented client-side.
    ats_score = models.PositiveSmallIntegerField(
        _("ATS score"), null=True, blank=True
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = OrgScopedManager(user_field="user")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("document")
        verbose_name_plural = _("documents")
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ("-updated_at", "-id")

    def __str__(self) -> str:  # pragma: no cover - admin nicety
        return self.name


class ApplicationStatus(models.TextChoices):
    """Spec status flow, plus the async-send states from the outbox spec."""

    PENDING = "pending", _("pending")
    RUNNING = "running", _("running")
    REVIEW = "review", _("review")
    SENDING = "sending", _("sending")
    SENT = "sent", _("sent")
    FAILED = "failed", _("failed")
    INTERVIEW = "interview", _("interview")
    OFFER = "offer", _("offer")
    REJECTED = "rejected", _("rejected")


class Application(models.Model):
    """One member's application to one listing. Org-scoped.

    Holds everything the Review drawer edits — ``POST send/`` persists the
    edited payload here before sending (the audited contract fix).
    """

    org = models.ForeignKey(
        "tenancy.Organization",
        on_delete=models.CASCADE,
        related_name="applications",
        verbose_name=_("organization"),
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="applications",
        verbose_name=_("user"),
    )
    job_listing = models.ForeignKey(
        JobListing,
        on_delete=models.PROTECT,
        related_name="applications",
        verbose_name=_("job listing"),
    )
    status = models.CharField(
        _("status"),
        max_length=16,
        choices=ApplicationStatus.choices,
        default=ApplicationStatus.PENDING,
    )
    subject = models.CharField(_("subject"), max_length=255, blank=True)
    generated_content = models.TextField(_("generated content"), blank=True)
    generated_cover_letter = models.TextField(
        _("generated cover letter"), blank=True
    )
    notes = models.TextField(_("notes"), blank=True)
    documents = models.ManyToManyField(
        Document,
        through="ApplicationDocument",
        related_name="applications",
        verbose_name=_("documents"),
        blank=True,
    )
    sent_manually = models.BooleanField(_("sent manually"), default=False)
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    sent_via = models.ForeignKey(
        "outbox.MailboxConnection",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applications",
        verbose_name=_("sent via"),
    )
    created_at = models.DateTimeField(_("created at"), auto_now_add=True)
    updated_at = models.DateTimeField(_("updated at"), auto_now=True)

    objects = OrgScopedManager(user_field="user")
    all_objects = models.Manager()

    class Meta:
        verbose_name = _("application")
        verbose_name_plural = _("applications")
        default_manager_name = "objects"
        base_manager_name = "all_objects"
        ordering = ("-updated_at", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=["org", "user", "job_listing"],
                name="one_application_per_member_per_listing",
            )
        ]

    def __str__(self) -> str:  # pragma: no cover - admin nicety
        return f"{self.user} → {self.job_listing} [{self.status}]"


class ApplicationDocument(models.Model):
    """Through table so an attachment set is explicit and orderable."""

    application = models.ForeignKey(
        Application, on_delete=models.CASCADE, verbose_name=_("application")
    )
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, verbose_name=_("document")
    )
    position = models.PositiveSmallIntegerField(_("position"), default=0)

    class Meta:
        verbose_name = _("application document")
        verbose_name_plural = _("application documents")
        ordering = ("position", "id")
        constraints = [
            models.UniqueConstraint(
                fields=["application", "document"],
                name="one_attachment_per_document",
            )
        ]
