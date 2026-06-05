"""Appointment serializers — upgraded with booking engine fields."""
from __future__ import annotations

from rest_framework import serializers

from .models import Appointment
from payments.models import Payment


# -- Read serializers ---------------------------------------------------------

class AppointmentServiceSerializer(serializers.Serializer):
    """Compact service snapshot for appointment responses."""
    id = serializers.UUIDField()
    name = serializers.CharField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    duration_minutes = serializers.IntegerField()


class AppointmentSpecialistSerializer(serializers.Serializer):
    """Compact specialist snapshot for appointment responses."""
    id = serializers.UUIDField()
    display_name = serializers.CharField()
    avatar = serializers.ImageField(allow_null=True)
    address = serializers.CharField()


class PaymentShortSerializer(serializers.ModelSerializer):
    """Compact payment info nested in appointment responses."""
    class Meta:
        model = Payment
        fields = [
            'id', 'amount', 'status', 'specialist_income',
            'platform_fee', 'provider',
        ]


class AppointmentListSerializer(serializers.ModelSerializer):
    """Minimal appointment for list views."""
    service_name = serializers.CharField(source='service.name', read_only=True)
    specialist_name = serializers.CharField(
        source='specialist.display_name', read_only=True,
    )
    specialist_id = serializers.UUIDField(source='specialist.id', read_only=True)

    class Meta:
        model = Appointment
        fields = [
            'id', 'status', 'start_datetime', 'end_datetime',
            'price', 'service_name', 'specialist_id', 'specialist_name',
            'is_first_visit', 'created_at',
        ]


class AppointmentDetailSerializer(serializers.ModelSerializer):
    """Full appointment details with snapshots and payment."""
    service = AppointmentServiceSerializer(read_only=True)
    specialist = AppointmentSpecialistSerializer(read_only=True)
    client_id = serializers.UUIDField(source='client.id', read_only=True)
    payment = serializers.SerializerMethodField()

    class Meta:
        model = Appointment
        fields = [
            'id', 'status', 'start_datetime', 'end_datetime',
            'price', 'notes', 'cancellation_reason',
            'idempotency_key', 'is_first_visit',
            # Snapshot fields
            'snapshot_service_name', 'snapshot_duration_minutes',
            'snapshot_price', 'snapshot_commission_percent',
            'snapshot_specialist_income', 'snapshot_platform_fee',
            'snapshot_timezone',
            # Relations
            'client_id', 'specialist', 'service', 'payment',
            'created_at', 'updated_at',
        ]

    def get_payment(self, obj: Appointment) -> dict | None:
        payment = obj.payments.order_by('-created_at').first()
        if payment:
            return PaymentShortSerializer(payment).data
        return None


# -- Write serializers --------------------------------------------------------

class AppointmentCreateSerializer(serializers.Serializer):
    """
    Validates input for booking creation.
    Actual business logic is in CreateBookingService.
    """
    specialist_id = serializers.UUIDField()
    service_id = serializers.UUIDField()
    start_datetime = serializers.DateTimeField()
    notes = serializers.CharField(required=False, allow_blank=True, default='')

    # Pure validator post-1.D. Variant E invisible-grant + F2 revoke
    # defense live in CreateBookingService._execute_atomic — folded
    # into the booking transaction so:
    # 1. The AI booking path (which bypasses this serializer) shares
    #    the same contract.
    # 2. If the booking rolls back later, the TUR write rolls back
    #    too — no orphan grants in the audit log.
    # 3. select_for_update on existing TUR rows defeats the admin-
    #    revoke TOCTOU race.


class WalkInCreateSerializer(serializers.Serializer):
    """Validates a provider walk-in booking (#1017).

    The specialist is taken from the authenticated user (a master books
    into their OWN diary), so only the service, slot, and the walk-in
    customer's name/phone are supplied. Validation only — the booking
    logic stays in CreateBookingService.
    """
    service_id = serializers.UUIDField()
    start_datetime = serializers.DateTimeField()
    client_name = serializers.CharField(max_length=150)
    client_phone = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=20,
    )


class AppointmentRescheduleSerializer(serializers.Serializer):
    """Validates input for rescheduling."""
    new_start_datetime = serializers.DateTimeField()


class AppointmentCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')
