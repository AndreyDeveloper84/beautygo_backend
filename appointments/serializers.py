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
        """Variant E — invisible TenantUserRelationship grant (#246 1.D).

        Replaces the Phase 0 cross-tenant 404 guard. When the customer
        books a specialist whose tenant they're not yet a member of,
        we invisibly create the relationship inside the booking
        transaction. The act of booking IS the consent gesture per
        `ayla-first-strategic-pivot` ("AI belongs to the user, salon
        is a provider"). No 404, no consent dialog.

        Defense from PR #152 F2: if a REVOKED TUR exists for the
        (user, specialist.tenant) pair, refuse silently with 404 —
        re-grant requires explicit admin action, not a side effect of
        the booking flow. Same posture as the User.post_save bridge.

        Permissive branches:
        - No request context / no `request.tenant`: caller didn't
          enter provider scope. CreateBookingService will stamp
          `appointment.tenant_id = specialist.tenant_id`; queryset
          scoping handles the rest. No TUR check.
        - Specialist not found: let downstream raise the proper error.
        - Specialist has no tenant (legacy NULL — pre-#590 row):
          legacy state, not a Variant E concern; let through.
        """
        request = self.context.get("request")
        if request is None:
            return attrs
        request_tenant = getattr(request, "tenant", None)
        if request_tenant is None:
            # Caller in global / customer-wide context (#246 design).
            # No tenant scope to bridge.
            return attrs

        from users.models import SpecialistProfile, TenantUserRelationship
        try:
            specialist = SpecialistProfile.objects.only(
                "tenant_id",
            ).get(pk=attrs["specialist_id"])
        except SpecialistProfile.DoesNotExist:
            return attrs

        if not specialist.tenant_id:
            # Legacy specialist with no tenant — Variant E only fires
            # for tenant-stamped specialists. Let downstream proceed.
            return attrs

        if specialist.tenant_id == request_tenant.id:
            # Same-tenant booking — no grant needed.
            return attrs

        # Cross-tenant: customer is in X-Tenant context for tenant A,
        # booking a specialist in tenant B. Need TUR(user, B).
        has_revoked = TenantUserRelationship.objects.filter(
            user=request.user,
            tenant_id=specialist.tenant_id,
            is_active=False,
        ).exists()
        if has_revoked:
            # F2 defense: refuse silently rather than silently re-grant.
            # Mirrors the User.post_save bridge revoke-defense logic.
            from rest_framework.exceptions import NotFound
            raise NotFound("Specialist not found.")

        # Invisible grant — get_or_create handles idempotency under
        # concurrent booking attempts. partial unique constraint on
        # (user, tenant) WHERE is_active=True catches dups via
        # IntegrityError if two concurrent grants race; get_or_create
        # surfaces that as "got" not "created."
        TenantUserRelationship.objects.get_or_create(
            user=request.user,
            tenant_id=specialist.tenant_id,
            is_active=True,
            defaults={
                "role": TenantUserRelationship.Role.CUSTOMER,
                "granted_by": TenantUserRelationship.GrantedBy.SELF,
            },
        )
        return attrs


class AppointmentRescheduleSerializer(serializers.Serializer):
    """Validates input for rescheduling."""
    new_start_datetime = serializers.DateTimeField()


class AppointmentCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')
