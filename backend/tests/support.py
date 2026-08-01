"""Shared test helpers.

``apps.accounts`` owns ``User`` and is built by a different work stream, so the
tenancy tests must not assume a particular manager signature. :func:`make_user`
adapts to whatever ``accounts.User`` turns out to be: email-as-username,
username-plus-email, ``create_user`` with or without a password argument.
"""

from __future__ import annotations

import itertools

from django.contrib.auth import get_user_model

_counter = itertools.count(1)


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}{next(_counter)}@example.de"


def make_user(email: str | None = None, **extra):
    """Create a user without caring how ``accounts.User`` is shaped."""
    User = get_user_model()
    email = email or unique_email()
    manager = User._default_manager

    username_field = getattr(User, "USERNAME_FIELD", "username")
    kwargs: dict = {username_field: email, **extra}
    if username_field != "email" and hasattr(User, "email"):
        kwargs.setdefault("email", email)

    for attempt in (
        lambda: manager.create_user(**kwargs),
        lambda: manager.create_user(password=None, **kwargs),
        lambda: manager.create(**kwargs),
    ):
        try:
            return attempt()
        except TypeError:
            continue
    raise RuntimeError(
        f"Could not create a {User._meta.label} instance with {sorted(kwargs)}."
    )
