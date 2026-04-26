"""DRF serializers for AI Chat endpoints.

Shapes mirror Notion API Spec v2.0 §AI ASSISTANT exactly. The view layer
wraps responses through ``users.response.success_response`` so the wire
format is ``{"data": {...}}``; serializers here produce the inner dict.
"""
from __future__ import annotations

from rest_framework import serializers

from ai.models import Conversation, Message
from ai.tools import ActionType


PREVIEW_TRUNCATE = 80
HISTORY_HARD_TRIM = 100


# ---------------------------------------------------------------------------
# Request payloads
# ---------------------------------------------------------------------------


class ChatRequestContextSerializer(serializers.Serializer):
    location = serializers.DictField(required=False, allow_null=True)
    preferred_date = serializers.DateField(required=False, allow_null=True)
    preferred_time = serializers.ChoiceField(
        choices=("morning", "afternoon", "evening"),
        required=False, allow_null=True,
    )

    def validate_location(self, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise serializers.ValidationError("Must be an object")
        try:
            lat = float(value.get("lat"))
            lon = float(value.get("lon"))
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError(
                "location.lat and location.lon must be numbers"
            ) from exc
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise serializers.ValidationError("lat/lon out of range")
        return {"lat": lat, "lon": lon}


class ChatRequestSerializer(serializers.Serializer):
    conversation_id = serializers.UUIDField(required=False, allow_null=True)
    message = serializers.CharField(min_length=1, max_length=4000)
    voice_mode = serializers.BooleanField(required=False, default=False)
    context = ChatRequestContextSerializer(required=False, allow_null=True)


class ActionRequestSerializer(serializers.Serializer):
    action_type = serializers.CharField(max_length=32)
    confirmed = serializers.BooleanField()
    data = serializers.DictField(required=False, allow_null=True)

    def validate_action_type(self, value):
        if value not in ActionType.ALL_MVP:
            raise serializers.ValidationError(
                f"Unknown or unsupported action_type: {value}"
            )
        return value


# ---------------------------------------------------------------------------
# Response shapes
# ---------------------------------------------------------------------------


class MessageSerializer(serializers.ModelSerializer):
    """Compact message — used in chat response."""

    class Meta:
        model = Message
        fields = ("id", "role", "content", "created_at")


class MessageFullSerializer(serializers.ModelSerializer):
    """Full message with action attached — used in conversation detail.

    Spec ``AIMessageFull.action`` is nullable; we emit the field only when
    role=assistant AND action_type is set.
    """

    action = serializers.SerializerMethodField()

    class Meta:
        model = Message
        fields = ("id", "role", "content", "action", "created_at")

    def get_action(self, obj: Message):
        if obj.role != Message.Role.ASSISTANT or not obj.action_type:
            return None
        return {"type": obj.action_type, "data": obj.action_data or {}}


class ConversationListItemSerializer(serializers.ModelSerializer):
    preview = serializers.SerializerMethodField()
    messages_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Conversation
        fields = (
            "id", "preview", "messages_count",
            "last_message_at", "created_at",
        )

    def get_preview(self, obj: Conversation) -> str:
        first = (
            obj.messages.filter(role=Message.Role.USER)
            .order_by("created_at")
            .first()
        )
        if first is None:
            return ""
        text = first.content or ""
        if len(text) > PREVIEW_TRUNCATE:
            return text[: PREVIEW_TRUNCATE - 1] + "…"
        return text


class ConversationDetailSerializer(serializers.ModelSerializer):
    """Full conversation with embedded messages (per spec)."""

    messages = serializers.SerializerMethodField()
    truncated = serializers.SerializerMethodField()

    class Meta:
        model = Conversation
        fields = ("id", "messages", "truncated", "created_at")

    def get_messages(self, obj: Conversation):
        qs = obj.messages.order_by("created_at")
        # Trim to last HISTORY_HARD_TRIM if exceeded.
        total = qs.count()
        if total > HISTORY_HARD_TRIM:
            qs = qs.order_by("-created_at")[:HISTORY_HARD_TRIM]
            ordered = sorted(qs, key=lambda m: m.created_at)
            return MessageFullSerializer(ordered, many=True).data
        return MessageFullSerializer(qs, many=True).data

    def get_truncated(self, obj: Conversation) -> bool:
        return obj.messages.count() > HISTORY_HARD_TRIM
