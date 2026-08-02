from django.contrib import admin

from apps.jobs.models import Application, Document, JobListing, Platform


@admin.register(Platform)
class PlatformAdmin(admin.ModelAdmin):
    list_display = (
        "slug", "name", "connector_type", "apply_mechanism", "risk",
        "auto_send", "is_active",
    )
    list_filter = ("risk", "auto_send", "is_active", "apply_mechanism")


@admin.register(JobListing)
class JobListingAdmin(admin.ModelAdmin):
    list_display = ("title", "company", "platform", "scraped_at")
    list_filter = ("platform",)
    search_fields = ("title", "company")
    date_hierarchy = "scraped_at"


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "org", "user", "ats_score", "updated_at")
    list_filter = ("kind",)
    search_fields = ("name", "user__email")

    def get_queryset(self, request):
        return Document.all_objects.all()


@admin.register(Application)
class ApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "job_listing", "user", "org", "status", "sent_manually", "sent_at"
    )
    list_filter = ("status", "sent_manually")
    search_fields = ("job_listing__title", "job_listing__company", "user__email")
    readonly_fields = ("sent_at", "sent_via", "created_at", "updated_at")

    def get_queryset(self, request):
        return Application.all_objects.all()
