from django.apps import AppConfig


class OutboxConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.tenancy.outbox"
    # Pinned, not inferred. The package moved from apps/outbox to
    # apps/tenancy/outbox, and the app label is what django_migrations rows and
    # every ForeignKey string reference are keyed on. The default label happens to
    # come out as "outbox" here too, but leaving it implicit means a future rename
    # of the directory silently orphans the migration history and Django tries to
    # re-run 0001_initial against tables that already exist.
    label = "outbox"
    verbose_name = "Outbox"
