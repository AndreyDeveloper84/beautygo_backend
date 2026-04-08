"""Payment serializers — aligned with API Specification v2.0."""
from __future__ import annotations

from rest_framework import serializers

from appointments.models import Payment

# Spec PaymentStatus: pending | succeeded | failed | refunded
# Internal statuses that don't exist in spec are mapped to closest match.
_STATUS_TO_SPEC = {
    Payment.Status.PENDING: 'pending',
    Payment.Status.AUTHORIZED: 'pending',        # hold in progress → still pending
    Payment.Status.PAID: 'succeeded',
    Payment.Status.FAILED: 'failed',
    Payment.Status.REFUNDED: 'refunded',
    Payment.Status.PARTIALLY_REFUNDED: 'refunded',
}


class PaymentCreateSerializer(serializers.Serializer):
    """POST /api/v1/payments/create — input."""
    appointment_id = serializers.UUIDField()
    return_url = serializers.URLField(
        required=False,
        default='https://beautygo.ru/payment/success',
    )


class PaymentDetailSerializer(serializers.ModelSerializer):
    """
    GET /api/v1/payments/{id} — matches Notion spec Payment model.

    Spec fields: id, appointment_id, client_id, amount, status,
                 provider, external_id, created_at, completed_at
    """
    appointment_id = serializers.UUIDField(source='appointment.id', read_only=True)
    client_id = serializers.UUIDField(source='appointment.client_id', read_only=True)
    status = serializers.SerializerMethodField()
    external_id = serializers.CharField(source='provider_payment_id', read_only=True)
    completed_at = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = [
            'id', 'appointment_id', 'client_id', 'amount', 'status',
            'provider', 'external_id', 'created_at', 'completed_at',
        ]

    def get_status(self, obj: Payment) -> str:
        return _STATUS_TO_SPEC.get(obj.status, obj.status)

    def get_completed_at(self, obj: Payment) -> str | None:
        if obj.status in (Payment.Status.PAID, Payment.Status.REFUNDED,
                          Payment.Status.PARTIALLY_REFUNDED):
            return obj.updated_at.isoformat() if obj.updated_at else None
        return None


class PaymentRefundSerializer(serializers.Serializer):
    """POST /api/v1/payments/{id}/refund — input."""
    amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False,
        help_text='Refund amount (defaults to full payment amount)',
    )
