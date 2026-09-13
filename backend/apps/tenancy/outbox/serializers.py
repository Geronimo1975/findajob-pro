"""Outbox serializers — secrets go in, never come back out."""

from __future__ import annotations

from rest_framework import serializers

from apps.tenancy.outbox.models import MailboxConnection, Provider


class MailboxSerializer(serializers.ModelSerializer):
    """Read shape. ``credentials_encrypted`` is deliberately absent."""

    class Meta:
        model = MailboxConnection
        fields = [
            "provider",
            "email_address",
            "display_name",
            "status",
            "last_verified_at",
            "last_error_code",
        ]

    def to_representation(self, instance):
        data = super().to_representation(instance)
        data["last_error_code"] = data["last_error_code"] or None
        return data


class SmtpConfigSerializer(serializers.Serializer):
    host = serializers.CharField(max_length=253)
    port = serializers.IntegerField(min_value=1, max_value=65535)
    username = serializers.CharField(max_length=320)
    password = serializers.CharField(max_length=1024)
    use_tls = serializers.BooleanField(default=True)


class ConnectMailboxSerializer(serializers.Serializer):
    """POST body for connecting a mailbox. Phase 1 accepts SMTP only."""

    provider = serializers.ChoiceField(choices=Provider.values)
    email_address = serializers.EmailField()
    display_name = serializers.CharField(
        max_length=200, required=False, allow_blank=True, default=""
    )
    smtp = SmtpConfigSerializer(required=False)

    def validate(self, attrs):
        if attrs["provider"] != Provider.SMTP:
            raise serializers.ValidationError(
                {"provider": "Only the smtp provider is available yet."}
            )
        if not attrs.get("smtp"):
            raise serializers.ValidationError(
                {"smtp": "SMTP settings are required for the smtp provider."}
            )
        return attrs
