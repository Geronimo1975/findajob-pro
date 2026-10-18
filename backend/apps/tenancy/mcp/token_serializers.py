from __future__ import annotations

from rest_framework import serializers

from apps.tenancy.mcp.models import DEFAULT_TTL_DAYS, SCOPES, McpToken


class McpTokenSerializer(serializers.ModelSerializer):
    status = serializers.CharField(source="status_code", read_only=True)

    class Meta:
        model = McpToken
        fields = ("id", "name", "prefix", "scopes", "expires_at",
                  "last_used_at", "status", "created_at")
        read_only_fields = fields


class CreateMcpTokenSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    scopes = serializers.ListField(
        child=serializers.ChoiceField(choices=SCOPES), allow_empty=False,
    )
    expires_in_days = serializers.IntegerField(
        min_value=1, max_value=365, default=DEFAULT_TTL_DAYS,
    )

    def validate_scopes(self, value):
        return sorted(set(value), key=SCOPES.index)
