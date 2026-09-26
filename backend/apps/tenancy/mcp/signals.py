"""Token lifecycle signals (spec §4, controller ruling R9).

Deleting a membership revokes every live MCP token the user holds in that
organization. Revocation is permanent: re-adding the same user later must not
resurrect a PAT minted under the old membership.
"""
from __future__ import annotations

from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

from apps.tenancy.mcp.models import McpToken
from apps.tenancy.models import Membership


@receiver(post_delete, sender=Membership, dispatch_uid="mcp_revoke_tokens_on_membership_delete")
def revoke_tokens_on_membership_delete(sender, instance, **kwargs) -> None:
    McpToken.objects.for_org(instance.org_id).filter(
        user_id=instance.user_id, revoked_at__isnull=True
    ).update(revoked_at=timezone.now())
