"""Request validators for the analytics ingestion endpoint.

Single shape — `EventCreateSerializer`. Mobile sends:

    {
      "event_name": "ai_chat_message_sent",
      "client_event_id": "<uuid>",
      "client_timestamp": "2026-05-05T14:00:00Z",
      "payload": { ... }
    }

The serializer validates `event_name` against
`analytics.event_catalogue.EVENT_NAMES` and shape-checks the
top-level fields. Per-event payload schemas are NOT validated here —
schema-on-read in BI is intentional (see model docstring).
"""
from __future__ import annotations

from rest_framework import serializers

from analytics.event_catalogue import EVENT_NAMES


class EventCreateSerializer(serializers.Serializer):
    event_name = serializers.CharField(max_length=64)
    client_event_id = serializers.UUIDField()
    client_timestamp = serializers.DateTimeField(required=False, allow_null=True)
    payload = serializers.JSONField(required=False, default=dict)

    def validate_event_name(self, value: str) -> str:
        if value not in EVENT_NAMES:
            raise serializers.ValidationError(
                f"Unknown event_name '{value}'. Add to "
                "analytics/event_catalogue.py or fix the mobile client."
            )
        return value

    def validate_payload(self, value):
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("payload must be an object")
        return value
