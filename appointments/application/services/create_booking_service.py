"""
CreateBookingService — the critical write path of the Booking Engine.

Steps:
1-4: Pre-transaction validation (cheap; no locks)
5-12: Atomic write path (inside transaction)
13: Post-commit side effects (via outbox worker)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as _dt_timezone

from django.conf import settings
from django.db import transaction

from appointments.application.dto import CreateBookingDTO, BookingResultDTO
from appointments.domain.exceptions import (
    BookingWindowError,
    ExternalSlotTakenError,
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
    OperationalActor,
    TimeInterval,
    ACTIVE_BOOKING_STATUSES,
    booking_source_for,
    envelope_actor_for,
)

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(tz=_dt_timezone.utc)


# The client actor has two spellings on CreateBookingDTO: "user" (legacy,
# predates the OperationalActor vocabulary) and "client". Both mean
# "the customer booking for themselves" and both get the full client
# time contract (DRF-1072).
_CLIENT_ACTOR_ROLES = frozenset({"user", OperationalActor.CLIENT.value})


def _is_client_actor(actor_role: str) -> bool:
    return actor_role in _CLIENT_ACTOR_ROLES


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
        specialist, resolved = self._validate_pre_transaction(dto)

        end_at = dto.start_at + timedelta(minutes=resolved.duration_minutes)
        target_interval = TimeInterval(start_at=dto.start_at, end_at=end_at)

        # AMD-019 persistence option A (v1.13.0): the booking carries
        # EXACTLY ONE typed service reference. Marketplace branch → the
        # marketplace Service row (the atomic write path keeps its
        # original shape for direct callers); salon branch →
        # Appointment.salon_service (by id, from the resolver).
        from services.models import Service
        service = None
        salon_service_id = None
        if resolved.kind == "marketplace":
            service = Service.objects.get(
                id=resolved.service_id, specialist=specialist,
            )
        else:
            salon_service_id = resolved.service_id

        appointment, payment = self._execute_atomic(
            dto=dto,
            specialist=specialist,
            service=service,
            target_interval=target_interval,
            resolved=resolved,
            salon_service_id=salon_service_id,
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
            service_name=resolved.name,
            duration_minutes=resolved.duration_minutes,
            price=str(resolved.price),
            payment_id=payment.id if payment else None,
            payment_client_secret=None,
        )

    def _validate_pre_transaction(self, dto: CreateBookingDTO):
        from users.models import SpecialistProfile

        try:
            specialist = SpecialistProfile.objects.select_related("user").get(
                id=dto.specialist_id
            )
        except SpecialistProfile.DoesNotExist:
            raise SpecialistNotActiveError(
                f"Specialist {dto.specialist_id} not found"
            )

        if not specialist.is_booking_enabled:
            raise SpecialistNotActiveError("Specialist is not accepting bookings")

        if specialist.status != SpecialistProfile.ProfileStatus.ACTIVE:
            raise SpecialistNotActiveError("Specialist profile is not active")

        # AMD-019 — shared resolver (services/service_resolver.py), the
        # SAME one the internal slots path uses: marketplace Service
        # first (UUID priority), then SalonService with an active
        # SpecialistService link in the booking tenant context.
        from services.service_resolver import (
            ServiceUnavailableForSpecialistError,
            resolve_bookable_service,
        )
        try:
            resolved = resolve_bookable_service(
                service_id=dto.service_id,
                specialist=specialist,
                tenant=specialist.tenant,
            )
        except ServiceUnavailableForSpecialistError:
            # Same public shape as before: marketplace-inactive and
            # missing/unlinked/unavailable all map to the existing
            # 422 SERVICE_NOT_ACTIVE (no existence leak).
            raise ServiceNotActiveError(
                "This service is not available for booking"
            )

        if resolved.duration_minutes is None:
            # Degenerate catalog row (no duration anywhere in the
            # cascade) — cannot price a slot; treat as unavailable.
            raise ServiceNotActiveError(
                "This service is not available for booking"
            )

        if dto.start_at.tzinfo is None:
            # Checked before any comparison against "now" below: a naive
            # datetime used to reach the window policy and die there with
            # a raw TypeError instead of this sentence.
            raise ValueError("start_at must be timezone-aware (UTC)")

        if dto.time_override:
            self._validate_time_override(dto)
        elif _is_client_actor(dto.actor_role):
            # DRF-1072: the whole booking window — the 60-minute notice
            # AND the published horizon — is the CLIENT's rule.
            self._booking_window_policy.validate_booking_window(dto.start_at)
        else:
            # DRF-1072: staff lose the notice, keep the outer bounds.
            # A master recording someone physically present at 19:30 is
            # not bound by a 60-minute self-service notice any more than
            # by the working-hours template (DRF-1062) — the schedule
            # constrains who may book the salon, not the salon itself.
            # But dropping the whole window with the notice would have
            # been a different thing entirely: nothing would then bound
            # a staff timestamp at all, and "any instant the caller
            # cares to name" is not a context — it is the absence of
            # one, which is exactly what this task exists to remove.
            self._validate_staff_time_bounds(dto.start_at)

        return specialist, resolved

    @staticmethod
    def _validate_time_override(dto: CreateBookingDTO) -> None:
        """Contract of the administrative override (DRF-1072).

        Explicit and trusted-caller only: never for the client actor,
        never without a reason. Violations are caller bugs, not user
        input problems — hence ValueError, same as the tz-aware check.
        """
        if _is_client_actor(dto.actor_role):
            raise ValueError(
                "time override is not available for client bookings"
            )
        if not (dto.time_override_reason or "").strip():
            raise ValueError("time override requires a reason")

    @staticmethod
    def _validate_staff_time_bounds(start_at) -> None:
        """The outer time bounds a staff booking keeps (DRF-1072).

        Everything the client window enforces EXCEPT the min-ahead
        notice: a booking is a future appointment, and it sits inside
        the horizon the marketplace publishes. Reads the same setting
        the client window reads, so the two cannot drift apart.

        Backdating a visit that already happened is a real need and a
        different act — it goes through the administrative override,
        explicitly and with a reason, never as a side effect of which
        endpoint the caller reached.
        """
        max_ahead = int(getattr(settings, "BOOKING_MAX_AHEAD_DAYS", 60))
        now = _now_utc()
        if start_at < now:
            raise BookingWindowError("Booking cannot be created in the past")
        if start_at > now + timedelta(days=max_ahead):
            raise BookingWindowError(
                f"Booking cannot be more than {max_ahead} days in advance"
            )

    @transaction.atomic
    def _execute_atomic(
        self,
        dto,
        specialist,
        service,
        target_interval: TimeInterval,
        *,
        resolved=None,
        salon_service_id=None,
    ):
        """Atomic write path.

        ``service`` is the marketplace Service for marketplace bookings
        (and the ONLY argument direct callers pass — tests build bookings
        this way). ``resolved`` + ``salon_service_id`` are the AMD-019
        additions: when ``resolved`` is given the snapshot stamps from it
        (identical fields for marketplace), and salon-catalog bookings
        persist with ``Appointment.salon_service`` instead of
        ``Appointment.service``.
        """
        from appointments.models import Appointment, SpecialistTimeOff
        from payments.models import Payment

        # Advisory lock (Wave 1 Simple Reschedule hardening) — serialises
        # all create/reschedule attempts for this specialist so the
        # conflict check below can't race an empty-slot phantom insert
        # (select_for_update on the conflict queryset locks nothing when
        # no overlapping row exists yet). See infrastructure/db_locks.py.
        from appointments.infrastructure.db_locks import specialist_advisory_lock
        specialist_advisory_lock(dto.specialist_id)

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

        # C1 — billing eligibility (W2): a past_due subscription blocks
        # only NEW bookings. Placed AFTER the idempotency early-return
        # so a retried create of an existing booking is never refused
        # (C1: only creation is gated); cancel/reschedule/complete never
        # consult this. Fail-open per C1 when billing is unavailable.
        # AMD-005: the billing account key is the Ayla User UUID
        # (specialist.user_id), NOT SpecialistProfile.id.
        from appointments.application.services.billing_eligibility import (
            check_billing_eligibility,
        )
        check_billing_eligibility(
            specialist_id=specialist.user_id,
            tenant_id=specialist.tenant_id,
        )

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

        # DRF-1072 — the time-context rule set now depends on the actor:
        #
        # * OVERRIDE (explicit, trusted-caller, reasoned — validated
        #   pre-transaction): lifts the whole set. The booking window was
        #   already skipped pre-transaction; grid/frame/closure/time-off
        #   are skipped here. The who/why is logged below and stamped
        #   onto the booking.created outbox payload. The conflict check
        #   above is NOT lifted — override rules on the time context,
        #   not on physical double-booking.
        # * CLIENT actor ("user"/"client"): the full contract — booking
        #   window (pre-transaction), slot grid (once the specialist has
        #   declared a schedule — same monotone rule as the frame),
        #   schedule frame, tenant closure, time-off. Until DRF-1072 the
        #   grid was the one rule the read path enforced and create did
        #   not.
        # * STAFF without override: time-off only. The schedule says when
        #   clients may book the salon, not when the salon may work — a
        #   walk-in is recorded by the master for someone physically
        #   present (DRF-1062), and the client self-service window is not
        #   their constraint either. Time-off still blocks: an absence is
        #   the master's own statement, so lifting it needs the explicit
        #   override, not a side effect of who pressed the button.
        if dto.time_override:
            logger.warning(
                "booking.time_override specialist=%s start=%s actor_role=%s "
                "actor_id=%s reason=%s",
                dto.specialist_id, target_interval.start_at.isoformat(),
                dto.actor_role, dto.actor_id, dto.time_override_reason,
            )
        else:
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

            # DRF-1062 — the schedule itself. Until this call, create
            # checked time-off but never the working day: a POST for a
            # Sunday landed even with Sunday marked non-working, because
            # SpecialistWorkingHours was read by the slot query and by
            # nothing else. The checks are shared with the reschedule
            # path so the two writers cannot drift apart again.
            if _is_client_actor(dto.actor_role):
                from appointments.application.services._booking_guards import (
                    check_grid_alignment,
                    check_schedule_frame,
                    check_tenant_closure,
                    schedule_declared,
                )
                # The grid is a property of the OFFERED slots: where the
                # specialist never declared a schedule nothing is offered
                # and there is no grid to violate — the same monotone-
                # enforcement rule check_schedule_frame follows (DRF-1062).
                # Gating here keeps the guard strictly additive for every
                # salon that has announced its hours, without turning
                # unconfigured specialists unbookable overnight.
                from zoneinfo import ZoneInfo
                local_date = target_interval.start_at.astimezone(
                    ZoneInfo(specialist.timezone)
                ).date()
                if schedule_declared(specialist, local_date):
                    check_grid_alignment(target_interval.start_at)
                check_schedule_frame(dto.specialist_id, target_interval)
                check_tenant_closure(dto.specialist_id, target_interval)

        # S3-CAL recheck-at-confirm (Level-1): external busy must be
        # re-validated inside this atomic block so an interval that arrived
        # after the read-path slot check still blocks the booking (TOCTOU-safe).
        # Inert when EXTERNAL_BUSY_ENABLED is off — booking behaviour unchanged.
        if getattr(settings, "EXTERNAL_BUSY_ENABLED", False):
            from services.models import ExternalBusyInterval
            external_busy = ExternalBusyInterval.objects.filter(
                specialist_id=dto.specialist_id,
                start_at__lt=target_interval.end_at,
                end_at__gt=target_interval.start_at,
            ).exists()
            if external_busy:
                raise ExternalSlotTakenError(
                    f"Slot {target_interval} is taken by an external calendar"
                )

        # First vs repeat visit
        is_first_visit = not Appointment.objects.filter(
            specialist_id=dto.specialist_id,
            client_id=dto.client_id,
            status=BookingStatus.COMPLETED.value,
        ).exists()

        # Platform fee snapshot (flat 90₽, AYLA-DEC-0001). With the
        # resolver present (the execute() path) the snapshot stamps from
        # the NORMALIZED resolution result — identical values for
        # marketplace bookings, the salon offer's own name/duration/
        # price for salon bookings. Direct callers without a resolver
        # keep the marketplace object fields.
        if resolved is not None:
            snapshot_name = resolved.name
            snapshot_duration = resolved.duration_minutes
            snapshot_price = resolved.price
            snapshot_buffer = resolved.buffer_after_minutes
        else:
            snapshot_name = service.name
            snapshot_duration = service.duration_minutes
            snapshot_price = service.price
            snapshot_buffer = getattr(service, "buffer_after_minutes", 0)
        platform_fee = self._commission_policy.get_platform_fee(snapshot_price)
        snapshot = BookingSnapshot.create(
            service_name=snapshot_name,
            duration_minutes=snapshot_duration,
            price=snapshot_price,
            platform_fee=platform_fee,
            specialist_timezone=specialist.timezone,
            buffer_after_minutes=snapshot_buffer,
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
        # the canonical "owner tenant" of the row. AMD-019 option A:
        # exactly one typed service reference — marketplace bookings
        # fill ``service``, salon-catalog bookings fill ``salon_service``
        # (the CHECK constraint enforces the XOR at the DB level).
        appointment = Appointment.objects.create(
            client_id=dto.client_id,
            specialist_id=dto.specialist_id,
            service_id=service.id if service is not None else None,
            salon_service_id=salon_service_id,
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
        # OperationalActor → ADR-0009 envelope actor. A provider-initiated
        # walk-in and a salon-recorded booking both map to "admin"; the
        # envelope is a coarse three-value enum by design and the finer
        # attribution travels in `source` below (event-contract §2.2).
        actor = envelope_actor_for(dto.actor_role)
        tenant_id = safe_tenant_id(appointment, context="booking.created")
        # DRF-1072 — override audit. The keys exist ONLY when the
        # administrative override was used, so an ordinary booking's
        # payload is byte-identical to before; a bypassed guard always
        # leaves a who (actor role + user id when known) and a why.
        override_audit = {}
        if dto.time_override:
            override_audit = {
                "time_override": True,
                "time_override_reason": dto.time_override_reason,
                "time_override_actor_id": (
                    str(dto.actor_id) if dto.actor_id else None
                ),
            }
        emit_outbox_event(
            topic=_OutboxEvent.Topic.BOOKING_CREATED,
            data={
                "appointment_id": str(appointment.id),
                "client_id": str(dto.client_id),
                "specialist_id": str(dto.specialist_id),
                "service_id": str(dto.service_id),
                "start_at": target_interval.start_at.isoformat(),
                "end_at": target_interval.end_at.isoformat(),
                # Lifecycle status the consumer mirrors onto
                # RemoteBookingProxy.status (event-contract.md §3.1; the
                # bot's consumers/booking.py hard-reads data["status"]).
                # Omitting it crashed the booking.created handler with
                # KeyError before delivery could ever succeed.
                "status": str(appointment.status),
                # Coarse origin channel (§3.1 `source`) — the `origin`
                # that Ayla MVP Appointment Contract §10 names as the
                # thing that distinguishes a manual salon booking from a
                # customer one. "walk_in" for a master-recorded booking,
                # "admin_console" when the salon books on a customer's
                # behalf, "mobile_app" for the customer themselves.
                # Consumer stores it on the proxy verbatim.
                "source": booking_source_for(dto.actor_role),
                # Contract field name for the booked total (§3.1
                # `price_total`). ``amount`` is kept below for the
                # in-process handlers that already read it.
                "price_total": str(snapshot.price),
                "payment_id": str(payment.id) if payment else None,
                "amount": str(snapshot.price),
                "specialist_timezone": snapshot.specialist_timezone,
                **override_audit,
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
