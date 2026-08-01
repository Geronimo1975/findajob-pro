from __future__ import annotations

from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "apps.accounts"
    label = "accounts"
    verbose_name = "Accounts & authentication"

    def ready(self) -> None:
        # Connects the Anymail tracking receiver that turns Brevo hard bounces
        # and complaints into SuppressedAddress rows (spec §6). Imported for the
        # side effect only.
        from apps.accounts import webhooks  # noqa: F401
