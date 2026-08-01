"""Tests for the Django admin surface.

``/admin/`` is exposed on the public hostname behind two independent gates
(Traefik basicAuth in front, Django superuser login behind), so the admin is not
"internal tooling nobody sees" — it is a production surface with production
data behind it. These tests cover the three things that break quietly:

* a ModelAdmin whose ``list_display`` names a field that no longer exists — the
  changelist 500s and nothing else notices;
* a read-only table that quietly becomes writable because a mixin was dropped;
* a secret rendered into a page because a ``readonly_fields`` entry was added
  without thinking about what it prints.
"""
