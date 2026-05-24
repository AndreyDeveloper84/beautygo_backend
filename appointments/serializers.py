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

    def validate(self, attrs):
        """Phase 0 (Variant B) interim cross-tenant guard.

        When the request carries a tenant context (X-Tenant header
        resolved by TenantContextMiddleware) and the requested
        specialist belongs to a DIFFERENT tenant, surface a 404
        "Specialist not found" instead of letting the booking land
        in the wrong tenant (post-#142 `IsTenantMember` accepts
        because the header matches the user; `CreateBookingService`
        then stamps `tenant_id = specialist.tenant_id`, and the
        strict queryset filter makes the row unreachable to the
        actor — confusing UX + mismatched billing tenant).

        Phase 1 will replace this with Variant E: invisibly create
        a TenantUserRelationship via Sprint 1 Track A #246 and let
        the booking proceed. See
        docs/design/cross-tenant-create-reject.md.
        """
        request = self.context.get("request")
        if request is None:
            return attrs
        request_tenant = getattr(request, "tenant", None)
        if request_tenant is None:
            # Permissive mode (no header) — Phase 0 freeze rollout.
            return attrs

        from users.models import SpecialistProfile
        try:
            specialist = SpecialistProfile.objects.only(
                "tenant_id",
            ).get(pk=attrs["specialist_id"])
        except SpecialistProfile.DoesNotExist:
            # Not the cross-tenant case — let downstream handle.
            return attrs

        if (
            specialist.tenant_id is not None
            and specialist.tenant_id != request_tenant.id
        ):
            from rest_framework.exceptions import NotFound
            raise NotFound("Specialist not found.")

        return attrs


class AppointmentRescheduleSerializer(serializers.Serializer):
    """Validates input for rescheduling."""
    new_start_datetime = serializers.DateTimeField()


class AppointmentCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')
