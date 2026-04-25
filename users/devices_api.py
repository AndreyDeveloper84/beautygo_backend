"""DeviceToken registration API.

Mobile apps register their FCM token at app launch (and on every token
refresh — Firebase rotates them). The endpoint upserts on the unique
``token`` column, so calling it repeatedly is safe.

Logout (``DELETE``) takes the row id rather than the raw token because
the mobile flow already has the id from the register response — and
treating tokens as opaque identifiers in URLs invites accidental
logging.
"""
from __future__ import annotations

from rest_framework import permissions, serializers, status
from rest_framework.generics import GenericAPIView
from rest_framework.request import Request
from rest_framework.response import Response

from .models import DeviceToken
from .response import error_response, success_response


class DeviceTokenRegisterSerializer(serializers.Serializer):
    token = serializers.CharField(max_length=255)
    app_type = serializers.ChoiceField(choices=DeviceToken.AppType.choices)
    platform = serializers.ChoiceField(choices=DeviceToken.Platform.choices)


class DeviceTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceToken
        fields = ["id", "token", "app_type", "platform", "is_active", "created_at"]
        read_only_fields = ["id", "created_at"]


class DeviceRegisterView(GenericAPIView):
    """POST /api/v1/devices/register/

    Idempotent — re-registering the same token attaches it to the
    current user (handles "user A logged out, user B logged in on the
    same device"). The unique constraint on ``token`` is honoured by
    the upsert path.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DeviceTokenRegisterSerializer

    def post(self, request: Request) -> Response:
        ser = self.get_serializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data

        device, created = DeviceToken.objects.update_or_create(
            token=data["token"],
            defaults={
                "user": request.user,
                "app_type": data["app_type"],
                "platform": data["platform"],
                "is_active": True,
            },
        )
        body = DeviceTokenSerializer(device).data
        return success_response(
            body,
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class DeviceDetailView(GenericAPIView):
    """DELETE /api/v1/devices/{id}/ — soft-deactivate (is_active=False).

    Soft delete keeps the row for audit and lets us re-activate the
    same token if the user logs back in. Owner-scoped — touching
    another user's device returns 404, not 403, so we don't leak
    existence.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = DeviceTokenSerializer

    def delete(self, request: Request, pk) -> Response:
        try:
            device = DeviceToken.objects.get(pk=pk, user=request.user)
        except DeviceToken.DoesNotExist:
            return error_response(
                code="NOT_FOUND",
                message="Device not found.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        device.is_active = False
        device.save(update_fields=["is_active"])
        return Response(status=status.HTTP_204_NO_CONTENT)
