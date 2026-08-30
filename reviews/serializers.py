"""Review serializers."""
from __future__ import annotations

from rest_framework import serializers

from .models import Review


class ReviewUpdateSerializer(serializers.Serializer):
    """PATCH /api/v1/reviews/{id}/ — edit text only."""
    text = serializers.CharField(
        required=False, allow_blank=True, max_length=1000,
    )


class ReviewReplySerializer(serializers.Serializer):
    """POST /api/v1/reviews/{id}/reply/ — specialist reply."""
    text = serializers.CharField(max_length=1000)


class ReviewCreateSerializer(serializers.Serializer):
    """POST /api/v1/reviews/ — input validation."""
    appointment_id = serializers.UUIDField()
    rating = serializers.IntegerField(min_value=1, max_value=5)
    text = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=1000,
    )
    is_anonymous = serializers.BooleanField(required=False, default=False)


class ReviewListSerializer(serializers.ModelSerializer):
    """Serializer for GET /specialists/{id}/reviews/ listing."""
    client_name = serializers.SerializerMethodField()
    # DRF-1421 — читаем разрешённое имя услуги, а не FK напрямую:
    # у салонного отзыва ``service`` NULL, и source='service.name'
    # отдал бы ``null`` вместо названия. Разрешение — на модели.
    service_name = serializers.CharField(read_only=True)

    class Meta:
        model = Review
        fields = [
            'id', 'rating', 'text', 'client_name',
            'service_name', 'specialist_reply',
            'is_anonymous', 'created_at',
        ]

    def get_client_name(self, obj: Review) -> str | None:
        if obj.is_anonymous:
            return None
        user = obj.client
        full = f"{user.first_name} {user.last_name}".strip()
        return full or user.username


class ReviewDetailSerializer(serializers.ModelSerializer):
    """Full review for POST response."""
    client_name = serializers.SerializerMethodField()
    # DRF-1421 — читаем разрешённое имя услуги, а не FK напрямую:
    # у салонного отзыва ``service`` NULL, и source='service.name'
    # отдал бы ``null`` вместо названия. Разрешение — на модели.
    service_name = serializers.CharField(read_only=True)
    appointment_id = serializers.UUIDField(source='appointment.id', read_only=True)

    class Meta:
        model = Review
        fields = [
            'id', 'appointment_id', 'rating', 'text',
            'client_name', 'service_name', 'is_anonymous',
            'specialist_reply', 'is_hidden', 'created_at',
        ]

    def get_client_name(self, obj: Review) -> str | None:
        if obj.is_anonymous:
            return None
        user = obj.client
        full = f"{user.first_name} {user.last_name}".strip()
        return full or user.username
