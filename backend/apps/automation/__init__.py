"""Read-only visibility into the automation layer (n8n).

This app owns **no models and no migrations**. It exists so the n8n instance
that runs findajob's scraping and generation workflows is visible from the same
place as the data it produces — the Django admin.

Hard boundary: everything here is **read-only**. The client issues ``GET``
requests and nothing else; there is no activate/deactivate, no trigger, no
workflow edit. If a write path is ever wanted it must be a deliberate, separate
decision, because the API token in play is a full-privilege n8n owner token.
"""
