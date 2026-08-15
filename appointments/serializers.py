"""Appointment serializers — upgraded with booking engine fields."""
from __future__ import annotations

import re

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
            # Closure attribution (DRF-1064). Additive: always present
            # (model default="") so existing consumers are unaffected,
            # and a client that wants to render "closed by the salon"
            # no longer has to infer it from the event stream.
            'completed_at', 'completed_by', 'no_show_marked_by',
            # Optimistic-concurrency counter (Wave 1). Always present
            # (model default=1) so exposing it here is a pure additive
            # change for every existing AppointmentDetailSerializer
            # consumer, not just reschedule responses.
            'version',
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
    # D6 — online payment is OPTIONAL. Default True preserves the
    # pre-pilot contract (AWAITING_PAYMENT + pending Payment). Passing
    # False books without prepayment: no Payment row, the booking lands
    # directly in CONFIRMED and booking.confirmed is emitted (R1).
    # The API layer derives confirm_immediately from this flag — the two
    # never diverge on public paths.
    payment_required = serializers.BooleanField(required=False, default=True)

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

    def validate_client_phone(self, value):
        """Normalise the walk-in phone to the SAME canonical form a
        registered account stores (``+7XXXXXXXXXX``).

        Registration runs every phone through ``PhoneSerializer`` →
        canonical ``+7…``. ``get_or_create_walkin_client`` excludes a
        real account by exact-string phone match, so if the walk-in
        number arrived in a different shape (``8…``, spaces, dashes) the
        exclusion would silently miss the real account and the stub would
        store a non-canonical duplicate of the same human's number. We
        canonicalise here so both sides compare in one form.

        Lenient on purpose: a walk-in is provider-entered free text, so a
        value that does NOT look like a RU mobile is cleaned of separators
        but accepted as-is rather than rejected — the master must still be
        able to record whatever contact they have. Empty stays empty.
        """
        if not value:
            return value
        cleaned = re.sub(r'[\s\-()]', '', value)
        if re.match(r'^(\+7|8)\d{10}$', cleaned):
            return '+7' + cleaned[1:] if cleaned.startswith('8') else cleaned
        return cleaned


class AppointmentRescheduleSerializer(serializers.Serializer):
    """Validates input for rescheduling."""
    new_start_datetime = serializers.DateTimeField()
    # Wave 1 Simple Reschedule — optional optimistic-concurrency check.
    # Omitted entirely by existing clients (backward compatible); when
    # present, RescheduleBookingService raises StaleVersionError (409)
    # if it no longer matches the appointment's current version.
    expected_version = serializers.IntegerField(required=False, min_value=1)


class InternalAppointmentRescheduleSerializer(AppointmentRescheduleSerializer):
    """Bot REST path variant of :class:`AppointmentRescheduleSerializer`.

    Wave 1 contract for ``ai-bot-platform`` Phase 2 (owner decision):
    ``expected_version`` is OPTIONAL for the existing mobile app (older
    client builds never send it), but REQUIRED for the new internal bot
    path — the bot always reads the appointment (and its current
    ``version``) immediately before offering a reschedule, so there is
    no legacy caller to stay compatible with here.
    """
    expected_version = serializers.IntegerField(required=True, min_value=1)


class AppointmentCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, default='')


class AppointmentCompleteSerializer(serializers.Serializer):
    """Validates ``POST /appointments/{id}/complete/`` and ``…/no-show/``.

    ``expected_version`` is the same optimistic-concurrency check Wave 1
    gave reschedule, extended to closure (DRF-1064). Optional, for the
    same reason it is optional on
    :class:`AppointmentRescheduleSerializer`: existing mobile builds and
    the ``PATCH /status/`` alias never send it, and turning a working
    call into a 400 is not the change this task is making. New surfaces
    — the master app and the salon console — are expected to always send
    it; they read the appointment (and its ``version``) immediately
    before offering the button.

    Note for callers: a successful closure does NOT bump ``version``
    (it is a reschedule counter — see ``Appointment.version``), so a
    replayed close with the same ``expected_version`` passes the version
    check and fails on state instead: 422 ``INVALID_STATUS``, not 409
    ``STALE_VERSION``. Both mean "re-read the appointment"; only the
    second means "someone moved it".
    """
    expected_version = serializers.IntegerField(required=False, min_value=1)
