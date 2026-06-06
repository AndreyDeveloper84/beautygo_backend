"""
CreateBookingService — the critical write path of the Booking Engine.

Steps:
1-4: Pre-transaction validation (cheap; no locks)
5-12: Atomic write path (inside transaction)
13: Post-commit side effects (via outbox worker)
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.db import transaction

from appointments.application.dto import CreateBookingDTO, BookingResultDTO
from appointments.domain.exceptions import (
    SlotNotAvailableError,
    SpecialistNotActiveError,
    ServiceNotActiveError,
)
from appointments.domain.policies import (
    CommissionPolicy,
    BookingWindowPolicy,
    DefaultCommissionPolicy,
    DefaultBookingWindowPolicy,
)
from appointments.domain.value_objects import (
    BookingStatus,
    BookingSnapshot,
    TimeInterval,
    ACTIVE_BOOKING_STATUSES,
)

logger = logging.getLogger(__name__)


class CreateBookingService:
    """Orchestrates the booking creation use case."""

    def __init__(
        self,
        commission_policy: CommissionPolicy | None = None,
        booking_window_policy: BookingWindowPolicy | None = None,
    ) -> None:
        self._commission_policy = commission_policy or DefaultCommissionPolicy()
        self._booking_window_policy = booking_window_policy or DefaultBookingWindowPolicy()

    def execute(self, dto: CreateBookingDTO) -> BookingResultDTO:
        specialist, service = self._validate_pre_transaction(dto)

        end_at = dto.start_at + timedelta(minutes=service.duration_minutes)
        target_interval = TimeInterval(start_at=dto.start_at, end_at=end_at)

        appointment, payment = self._execute_atomic(
            dto=dto,
            specialist=specialist,
            service=service,
            target_interval=target_interval,
        )

        logger.info(
            "booking.created booking_id=%s specialist=%s start=%s",
            appointment.id, dto.specialist_id, dto.start_at.isoformat(),
        )

        return BookingResultDTO(
            booking_id=appointment.id,
            status=appointment.status,
            start_at=appointment.start_datetime,
            end_at=appointment.end_datetime,
            service_name=service.name,
            duration_minutes=service.duration_minutes,
            price=str(service.price),
            payment_id=payment.id if payment else None,
            payment_client_secret=None,
        )

    def _validate_pre_transaction(self, dto: CreateBookingDTO):
        from users.models import SpecialistProfile
        from services.models import Service

        try:
            specialist = SpecialistProfile.objects.select_related("user").get(
                id=dto.specialist_id
            )
        except SpecialistProfile.DoesNotExist:
            raise SpecialistNotActiveError(
                f"Specialist {dto.specialist_id} not found"
            )

        try:
            service = Service.objects.get(
                id=dto.service_id, specialist=specialist,
            )
        except Service.DoesNotExist:
            raise ServiceNotActiveError(
                f"Service {dto.service_id} not found"
            )

        if not specialist.is_booking_enabled:
            raise SpecialistNotActiveError("Specialist is not accepting bookings")

        if specialist.status != SpecialistProfile.ProfileStatus.ACTIVE:
            raise SpecialistNotActiveError("Specialist profile is not active")

        if not service.is_active:
            raise ServiceNotActiveError("This service is not available for booking")

        self._booking_window_policy.validate_booking_window(dto.start_at)

        if dto.start_at.tzinfo is None:
            raise ValueError("start_at must be timezone-aware (UTC)")

        return specialist, service

    @transaction.atomic
    def _execute_atomic(self, dto, specialist, service, target_interval: TimeInterval):
        from appointments.models import Appointment, SpecialistTimeOff
        from payments.models import Payment

        # Idempotency check
        existing = Appointment.objects.filter(
            idempotency_key=dto.idempotency_key
        ).select_for_update().first()

        if existing:
            logger.info(
                "booking.idempotent_return booking_id=%s key=%s",
                existing.id, dto.idempotency_key,
            )
            return existing, existing.payments.filter(status="pending").first()

        # Conflict check with row-level lock
        conflicting_count = Appointment.objects.filter(
            specialist_id=dto.specialist_id,
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
            start_datetime__lt=target_interval.end_at,
            end_datetime__gt=target_interval.start_at,
        ).select_for_update().count()

        if conflicting_count > 0:
            raise SlotNotAvailableError(
                f"Slot {target_interval} is already taken"
            )

        # Check time-off blocks
        blocked = SpecialistTimeOff.objects.filter(
            specialist_id=dto.specialist_id,
            start_at__lt=target_interval.end_at,
            end_at__gt=target_interval.start_at,
        ).exists()

        if blocked:
            raise SlotNotAvailableError(
                f"Slot {target_interval} is blocked by specialist"
            )

        # First vs repeat visit
        is_first_visit = not Appointment.objects.filter(
            specialist_id=dto.specialist_id,
            client_id=dto.client_id,
            status=BookingStatus.COMPLETED.value,
        ).exists()

        # Commission snapshot
        commission_percent = Decimal(str(
            self._commission_policy.get_percent(dto.client_id, dto.specialist_id)
        ))
        snapshot = BookingSnapshot.create(
            service_name=service.name,
            duration_minutes=service.duration_minutes,
            price=service.price,
            commission_percent=commission_percent,
            specialist_timezone=specialist.timezone,
            buffer_after_minutes=getattr(service, "buffer_after_minutes", 0),
        )

        # Grant-on-first-booking — single platform-wide rule (#1014).
        # "Booking IS the consent gesture": any successful booking
        # through any channel (mobile, internal/bot REST, provider
        # walk-in) locks the customer to the specialist's tenant the
        # first time they book there. This generalises the original
        # #246 Variant E grant, which only fired in the cross-tenant
        # case (request_tenant_id set AND ≠ specialist tenant). The
        # nationwide bot books with no client tenant context
        # (tenant_id claim is None for customers per get_jwt_tenant_claim),
        # so a request_tenant_id-gated grant never fired for it — the
        # gate is removed and the grant now keys purely off the
        # specialist's tenant.
        #
        # Folded into the booking transaction (not the serializer) so:
        #   1. Every create path gets it (bot/AI/walk-in bypass the
        #      serializer).
        #   2. If the booking rolls back later (slot conflict, etc.) the
        #      TUR write rolls back with it — no orphan grants.
        #   3. ``select_for_update`` on existing TUR rows defeats the
        #      admin-revoke TOCTOU race (PR #154 adversarial F-3).
        # Create-path only — never granted on a read/availability path.
        if specialist.tenant_id is not None:
            # Lock existing TUR rows for this (user, tenant) pair so an
            # admin revoke that lands between our read and write
            # serialises behind our row lock.
            from users.models import TenantUserRelationship
            existing = list(
                TenantUserRelationship.objects.select_for_update().filter(
                    user_id=dto.client_id,
                    tenant_id=specialist.tenant_id,
                )
            )
            has_revoked = any(not row.is_active for row in existing)
            if has_revoked:
                # F2 defense (PR #152): a revoked relationship refuses
                # silently — re-grant requires explicit admin action.
                # Preserved verbatim under the generalised rule, so a
                # banned customer cannot silently re-book via ANY
                # channel, same-tenant or cross-tenant.
                from rest_framework.exceptions import NotFound
                raise NotFound("Specialist not found.")
            has_active = any(row.is_active for row in existing)
            if not has_active:
                # TOCTOU note: the select_for_update above locks only
                # rows that ALREADY exist. On a true first booking there
                # are none, so two concurrent first-bookings for the same
                # (client, tenant) both read empty, both reach this insert.
                # The partial-unique constraint ``tur_unique_active``
                # rejects the loser with IntegrityError. Wrap the insert
                # in a savepoint so that rejection doesn't poison the
                # outer booking transaction: on conflict a concurrent
                # booking has already granted the (active) TUR, so the
                # grant intent is satisfied — the booking proceeds.
                from django.db import IntegrityError
                try:
                    with transaction.atomic():
                        TenantUserRelationship.objects.create(
                            user_id=dto.client_id,
                            tenant_id=specialist.tenant_id,
                            is_active=True,
                            role=TenantUserRelationship.Role.CUSTOMER,
                            granted_by=TenantUserRelationship.GrantedBy.SELF,
                        )
                except IntegrityError:
                    # Loser of the first-booking race. The winner's active
                    # TUR now exists (the partial-unique fires only on
                    # active rows), so the grant is effectively done.
                    logger.info(
                        "booking.tur_grant_race client=%s tenant=%s "
                        "— concurrent grant won, proceeding",
                        dto.client_id, specialist.tenant_id,
                    )

        # Provider walk-in (#1017): the cash/in-person transaction
        # happens off-platform, so the booking skips the online Payment
        # and lands directly in CONFIRMED. The default customer path
        # keeps the online-payment contract (AWAITING_PAYMENT + a pending
        # Payment row the YooKassa hold later confirms).
        initial_status = (
            BookingStatus.CONFIRMED.value
            if dto.confirm_immediately
            else BookingStatus.AWAITING_PAYMENT.value
        )

        # Create appointment. tenant_id mirrors specialist.tenant_id —
        # the canonical "owner tenant" of the row.
        appointment = Appointment.objects.create(
            client_id=dto.client_id,
            specialist_id=dto.specialist_id,
            service_id=dto.service_id,
            tenant_id=specialist.tenant_id,
            start_datetime=target_interval.start_at,
            end_datetime=target_interval.end_at,
            status=initial_status,
            idempotency_key=dto.idempotency_key,
            is_first_visit=is_first_visit,
            price=snapshot.price,
            snapshot_service_name=snapshot.service_name,
            snapshot_duration_minutes=snapshot.service_duration_minutes,
            snapshot_price=snapshot.price,
            snapshot_commission_percent=snapshot.commission_percent,
            snapshot_specialist_income=snapshot.specialist_income,
            snapshot_platform_fee=snapshot.platform_fee,
            snapshot_timezone=snapshot.specialist_timezone,
        )

        # Create payment record (online-payment path only). Walk-ins are
        # settled off-platform — no Payment row.
        payment = None
        if dto.payment_required:
            payment = Payment.objects.create(
                appointment=appointment,
                amount=snapshot.price,
                status="pending",
                specialist_income=snapshot.specialist_income,
                platform_fee=snapshot.platform_fee,
            )

        # Write outbox event(s) (same transaction). Envelope per ADR-0009
        # §Mandatory event contract — emit_outbox_event wraps the
        # domain data in the canonical envelope (event_id, event_version,
        # actor, correlation_id, tenant_id, …) so cross-service consumers
        # can dedupe + version-route handlers.
        from appointments.infrastructure.outbox import (
            emit_outbox_event, safe_tenant_id,
        )
        from appointments.models import OutboxEvent as _OutboxEvent
        # actor_role contract {user, specialist, system} → ADR-0009 actor.
        # A provider-initiated walk-in maps "specialist" → "admin".
        actor = (
            "admin" if dto.actor_role == "specialist"
            else "system" if dto.actor_role == "system"
            else "user"
        )
        tenant_id = safe_tenant_id(appointment, context="booking.created")
        emit_outbox_event(
            topic=_OutboxEvent.Topic.BOOKING_CREATED,
            data={
                "appointment_id": str(appointment.id),
                "client_id": str(dto.client_id),
                "specialist_id": str(dto.specialist_id),
                "service_id": str(dto.service_id),
                "start_at": target_interval.start_at.isoformat(),
                "end_at": target_interval.end_at.isoformat(),
                "payment_id": str(payment.id) if payment else None,
                "amount": str(snapshot.price),
                "specialist_timezone": snapshot.specialist_timezone,
            },
            user_id=dto.client_id,
            tenant_id=tenant_id,
            actor=actor,
        )
        # For a walk-in the booking is already CONFIRMED, so also emit
        # booking.confirmed — mirrors the lifecycle the YooKassa hold
        # produces for online bookings, keeping the bot's mirror +
        # reminder scheduling correct (a walk-in is now a second path,
        # besides payment hold, that reaches CONFIRMED).
        if initial_status == BookingStatus.CONFIRMED.value:
            emit_outbox_event(
                topic=_OutboxEvent.Topic.BOOKING_CONFIRMED,
                data={
                    "appointment_id": str(appointment.id),
                    "client_id": str(dto.client_id),
                    "specialist_id": str(dto.specialist_id),
                    "payment_id": str(payment.id) if payment else None,
                },
                user_id=dto.client_id,
                tenant_id=tenant_id,
                actor=actor,
            )

        return appointment, payment
