"""DRF serializers for notification list/read endpoints (Slice N3).

Mapping note: Notion API Spec v2.0 §NOTIFICATIONS Notification shape uses
``type`` for the event kind. The model column is ``template_id`` (granular,
e.g. ``appointment_created_client`` vs ``appointment_created_specialist``)
because we route templates per recipient app_type. The serializer maps
``template_id`` → ``type`` on the wire so mobile gets the spec shape.
"""
from __future__ import annotations

from rest_framework import serializers

from .models import Notification


class NotificationListItemSerializer(serializers.ModelSerializer):
    """Single Notification on the wire.

    Per spec: ``id, user_id, type, title, body, data, is_read, created_at``.
    Spec ``data`` is whatever the dispatcher put in ``Notification.data``
    at send time (template context dict — already serialised by the JSON
    field, no transformation needed).
    """

    type = serializers.CharField(source="template_id", read_only=True)
    user_id = serializers.UUIDField(read_only=True)

    class Meta:
        model = Notification
        fields = [
            "id",
            "user_id",
            "type",
            "title",
            "body",
            "data",
            "is_read",
            "created_at",
        ]
        read_only_fields = fields


class NotificationListQuerySerializer(serializers.Serializer):
    """Validate the ``is_read`` query param.

    DRF's PageNumberPagination handles ``page`` / ``page_size``. We only
    surface and validate ``is_read`` here so a typo (?is_read=tru) fails
    with a clear 400 instead of silently returning the unfiltered list.
    """

    is_read = serializers.BooleanField(required=False, allow_null=True)
