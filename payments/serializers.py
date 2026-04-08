"""Payment serializers."""
from __future__ import annotations

from rest_framework import serializers

from appointments.models import Payment


class PaymentCreateSerializer(serializers.Serializer):
    """POST /api/v1/payments/create — input."""
    appointment_id = serializers.UUIDField()
    return_url = serializers.URLField(
        required=False,
        default='https://beautygo.ru/payment/success',
    )


class PaymentDetailSerializer(serializers.ModelSerializer):
    """Full payment detail."""
    appointment_id = serializers.UUIDField(source='appointment.id', read_only=True)

    class Meta:
        model = Payment
        fields = [
            'id', 'appointment_id', 'amount', 'status',
            'specialist_income', 'platform_fee', 'refunded_amount',
            'provider', 'provider_payment_id',
            'created_at', 'updated_at',
        ]


class PaymentRefundSerializer(serializers.Serializer):
    """POST /api/v1/payments/{id}/refund — input."""
    amount = serializers.DecimalField(
        max_digits=10, decimal_places=2, required=False,
        help_text='Refund amount (defaults to full payment amount)',
    )
