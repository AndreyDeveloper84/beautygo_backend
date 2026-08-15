"""Appointment views — thin views delegating to application services."""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import permissions, viewsets
from rest_framework.decorators import action
from rest_framework.request import Request
from rest_framework.response import Response

from users.permissions import IsProApp, IsSpecialist, IsTenantMember
from users.response import error_response, success_response

from .application.dto import (
    CancelBookingDTO,
    CreateBookingDTO,
    RescheduleBookingDTO,
)
from .application.services.cancel_reschedule_service import (
    CancelBookingService,
    RescheduleBookingService,
)
from .application.services.completion import (
    close_booking,
    schedule_capture_safely,
)
from .application.services.create_booking_service import CreateBookingService
from .authz import may_operate_on_bookings, resolve_booking_operator
from .domain.exceptions import (
    AppointmentTerminalError,
    BookingWindowError,
    CancellationNotAllowedError,
    ExpectedVersionRequiredError,
    InvalidStateTransitionError,
    RescheduleNotAllowedError,
    SlotNotAvailableError,
    StaleVersionError,
    TenantMismatchError,
)
from .domain.value_objects import cancelled_by_for, envelope_actor_for
from .infrastructure.idempotency import (
    IdempotencyConflict,
    IdempotencyInFlight,
    lookup_or_open_idempotency,
    record_response,
)
from .infrastructure.outbox import emit_outbox_event, safe_tenant_id
from .models import Appointment, OutboxEvent
from .serializers import (
    AppointmentCancelSerializer,
    AppointmentCompleteSerializer,
    AppointmentCreateSerializer,
    AppointmentDetailSerializer,
    AppointmentListSerializer,
    AppointmentRescheduleSerializer,
    WalkInCreateSerializer,
)

logger = logging.getLogger(__name__)


class AppointmentViewSet(viewsets.GenericViewSet):
    """
    Appointments API.

    - POST   /api/v1/appointments/              Create (client only)
    - GET    /api/v1/appointments/              My appointments list
    - GET    /api/v1/appointments/{id}/         Detail
    - POST   /api/v1/appointments/{id}/cancel/  Cancel
    - POST   /api/v1/appointments/{id}/complete/ Complete (specialist only)
    - POST   /api/v1/appointments/{id}/reschedule/ Reschedule
    """
    # IsTenantMember closes the ADR-0009 §Hard rule #6 gap (#520) +
    # post-#246 sub-phase 1.B reads membership from TenantUserRelationship
    # instead of User.tenant FK. Post-1.C: permission stays here in
    # PERMISSIVE mode (returns True when request.tenant=None) so the
    # global multi-provider customer endpoints work — Anna can call
    # GET /api/v1/appointments/ without X-Tenant header and see her
    # bookings across all her active TUR tenants. When X-Tenant IS
    # set, IsTenantMember rejects callers without an active TUR for
    # that tenant. Sub-phase 1.D adds Variant E (invisible TUR grant)
    # in the AppointmentCreateSerializer.validate.
    permission_classes = [permissions.IsAuthenticated, IsTenantMember]

    # Service classes — override in tests for mocking
    create_booking_service_class = CreateBookingService
    cancel_booking_service_class = CancelBookingService
    reschedule_booking_service_class = RescheduleBookingService

    queryset = Appointment.objects.none()

    def get_queryset(self) -> QuerySet:
        # During drf-spectacular schema generation the viewset is invoked
        # without a real user. Returning an empty queryset early skips
        # the role-based filter (which would raise AttributeError on
        # AnonymousUser.is_client) and lets spectacular derive the
        # model type from the queryset attribute alone.
        if getattr(self, 'swagger_fake_view', False):
            return Appointment.objects.none()
        user = self.request.user
        qs = (
            Appointment.objects
            .select_related('client', 'specialist', 'service', 'service__category')
            .prefetch_related('payments')
        )

        # #520 ADR-0009 §Hard rule #6 — when the request carries a tenant
        # context (X-Tenant header or JWT tenant_id claim resolved by
        # TenantContextMiddleware), restrict the queryset to that tenant.
        # Without this filter, a multi-tenant specialist could see (and
        # therefore complete/cancel/reschedule) appointments belonging
        # to a tenant the request is NOT scoped to.
        #
        # Strict equality post-#568: legacy tenant_id=NULL rows are
        # backfilled by migration 0008_backfill_appointment_tenant. The
        # rollout-window Q(tenant__isnull=True) OR-clause has been
        # dropped — post-backfill any NULL is an integrity hole, not
        # legacy state. Pre-STRICT-flip 2026-05-28 hardening.
        request_tenant = getattr(self.request, "tenant", None)
        if request_tenant is not None:
            qs = qs.filter(tenant=request_tenant)

        if user.is_client:
            return qs.filter(client=user)
        if user.is_specialist:
            return qs.filter(specialist__user=user)
        return qs.none()

    def get_serializer_class(self):
        # Per-action serializer mapping. drf-spectacular uses this to
        # derive request/response shapes; without it the viewset has
        # no canonical ``serializer_class`` (because each action uses
        # a different serializer) and schema generation falls back
        # to error mode.
        if self.action == 'list':
            return AppointmentListSerializer
        if self.action == 'create':
            return AppointmentCreateSerializer
        if self.action == 'cancel':
            return AppointmentCancelSerializer
        if self.action == 'reschedule':
            return AppointmentRescheduleSerializer
        # retrieve, complete, update_status — full detail body
        return AppointmentDetailSerializer

    # -- List ----------------------------------------------------------------

    @extend_schema(
        operation_id="appointments_list",
        responses={200: AppointmentListSerializer(many=True)},
    )
    def list(self, request: Request) -> Response:
        from core.pagination import paginated_success_response

        qs = self.get_queryset()
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return paginated_success_response(
            qs, AppointmentListSerializer, request,
        )

    # -- Create (via booking engine) ----------------------------------------

    @extend_schema(
        operation_id="appointments_create",
        request=AppointmentCreateSerializer,
        responses={201: AppointmentDetailSerializer},
    )
    def create(self, request: Request) -> Response:
        if not request.user.is_client:
            return error_response(
                "FORBIDDEN", "Only clients can create appointments.",
                status_code=403,
            )

        # Serializer is now a pure validator post-1.D (no DB side
        # effects). Variant E invisible-grant moved into
        # CreateBookingService._execute_atomic so it shares the booking
        # transaction (rollback-safe + AI-path-compatible).
        serializer = AppointmentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        idempotency_key = request.META.get(
            'HTTP_X_IDEMPOTENCY_KEY', str(uuid4()),
        )
        request_tenant = getattr(request, "tenant", None)

        dto = CreateBookingDTO(
            client_id=request.user.id,
            specialist_id=serializer.validated_data['specialist_id'],
            service_id=serializer.validated_data['service_id'],
            start_at=serializer.validated_data['start_datetime'],
            idempotency_key=idempotency_key,
            request_tenant_id=(
                request_tenant.id if request_tenant else None
            ),
            # D6: payment_required=False → confirm immediately without a
            # Payment row (no-prepayment pilot baseline).
            payment_required=serializer.validated_data['payment_required'],
            confirm_immediately=(
                not serializer.validated_data['payment_required']
            ),
        )

        service = self.create_booking_service_class()
        from appointments.domain.exceptions import BillingEligibilityError
        try:
            result = service.execute(dto)
        except BillingEligibilityError:
            # C1 privacy rule: the CLIENT-facing API never discloses the
            # debt reason — generic UNAVAILABLE with a neutral message;
            # the master sees the debt screen in their own cabinet.
            return error_response(
                "UNAVAILABLE",
                "Сейчас запись к этому специалисту недоступна. "
                "Попробуйте выбрать другого мастера или другое время.",
                status_code=409,
            )

        # Reload for full serialization
        appointment = (
            Appointment.objects
            .select_related('client', 'specialist', 'service')
            .prefetch_related('payments')
            .get(id=result.booking_id)
        )
        return success_response(
            AppointmentDetailSerializer(appointment).data,
            status_code=201,
        )

    # -- Walk-in (provider records an off-platform booking) ------------------

    @extend_schema(
        operation_id="appointments_walk_in",
        request=WalkInCreateSerializer,
        responses={201: AppointmentDetailSerializer},
    )
    @action(
        detail=False, methods=['post'], url_path='walk-in',
        permission_classes=[permissions.IsAuthenticated, IsProApp, IsSpecialist],
    )
    def walk_in(self, request: Request) -> Response:
        """POST /api/v1/appointments/walk-in/ — a master records a
        walk-in (no app, no online payment) into their OWN diary.

        Without this the slot a walk-in occupies stays "free" in the
        bot's mirror and gets double-booked (#1017). The booking reuses
        the engine (conflict re-check, snapshot, grant, outbox) but skips
        Payment and lands directly in CONFIRMED, emitting booking.created
        + booking.confirmed so the mirror + reminders stay correct.
        """
        from users.services import get_or_create_walkin_client

        specialist = getattr(request.user, 'specialist_profile', None)
        if specialist is None:
            return error_response(
                "FORBIDDEN", "Specialist profile required.", status_code=403,
            )

        serializer = WalkInCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        client_name = serializer.validated_data['client_name']
        client_phone = serializer.validated_data.get('client_phone') or None
        walk_in_client = get_or_create_walkin_client(client_name, client_phone)

        idempotency_key = request.META.get(
            'HTTP_X_IDEMPOTENCY_KEY', str(uuid4()),
        )
        dto = CreateBookingDTO(
            client_id=walk_in_client.id,
            specialist_id=specialist.id,
            service_id=serializer.validated_data['service_id'],
            start_at=serializer.validated_data['start_datetime'],
            idempotency_key=idempotency_key,
            request_tenant_id=None,
            payment_required=False,
            confirm_immediately=True,
            actor_role="specialist",
        )

        # Booking domain errors (slot taken, inactive service) propagate
        # to api_exception_handler.
        result = self.create_booking_service_class().execute(dto)

        appointment = (
            Appointment.objects
            .select_related('client', 'specialist', 'service')
            .prefetch_related('payments')
            .get(id=result.booking_id)
        )
        # Mirror the walk-in customer's name + phone onto the booking for
        # the provider's reference. The phone is kept here unconditionally
        # because the proxy User may NOT carry it (when the number already
        # belongs to a real account, get_or_create_walkin_client leaves the
        # stub's phone NULL to avoid co-opting that account) — notes is then
        # the master's only record of how to reach the walk-in.
        if not appointment.notes:
            note = f"Walk-in: {client_name}"
            if client_phone:
                note += f" ({client_phone})"
            appointment.notes = note
            appointment.save(update_fields=['notes'])
        return success_response(
            AppointmentDetailSerializer(appointment).data,
            status_code=201,
        )

    # -- Retrieve ------------------------------------------------------------

    @extend_schema(
        operation_id="appointments_retrieve",
        responses={200: AppointmentDetailSerializer, 404: None},
    )
    def retrieve(self, request: Request, pk: Any = None) -> Response:
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )
        return success_response(AppointmentDetailSerializer(appointment).data)

    # -- Cancel (via booking engine) ----------------------------------------

    @extend_schema(
        request=AppointmentCancelSerializer,
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def cancel(self, request: Request, pk: Any = None) -> Response:
        # X-Idempotency-Key replay protection (#512). Header-optional —
        # clients without it pass through with no tracking. With it,
        # the helper raises IdempotencyConflict (different body) or
        # IdempotencyInFlight (placeholder still pending). On hit
        # we return cached; otherwise we open a placeholder record
        # that MUST be filled in via record_response on EVERY return
        # path (success AND error) — see Stripe-style semantics in
        # infrastructure/idempotency.py.
        try:
            cached, idem_record = lookup_or_open_idempotency(
                request,
                operation_name="booking.cancel",
                target_type="Appointment",
                target_id=str(pk),
            )
        except IdempotencyConflict as exc:
            return error_response(
                "IDEMPOTENCY_CONFLICT", str(exc), status_code=422,
            )
        except IdempotencyInFlight as exc:
            return error_response(
                "IDEMPOTENCY_IN_FLIGHT", str(exc), status_code=409,
            )
        if cached is not None:
            return Response(cached["payload"], status=cached["status"])

        response = self._cancel_inner(request, pk)
        if idem_record is not None:
            record_response(idem_record, response.status_code, response.data)
        return response

    def _cancel_inner(self, request: Request, pk: Any) -> Response:
        """Cancel implementation, isolated so the outer ``cancel`` can
        record_response on EVERY return without scattering the call.
        """
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )

        serializer = AppointmentCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        dto = CancelBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            initiator_role="specialist" if request.user.is_specialist else "client",
            reason=serializer.validated_data.get('reason', ''),
        )

        try:
            self.cancel_booking_service_class().execute(dto)
        except CancellationNotAllowedError as e:
            return error_response(
                "CANCELLATION_NOT_ALLOWED", str(e), status_code=422,
            )
        except InvalidStateTransitionError as e:
            return error_response(
                "INVALID_STATUS", str(e), status_code=422,
            )

        appointment.refresh_from_db()
        return success_response(AppointmentDetailSerializer(appointment).data)

    # -- Complete ------------------------------------------------------------

    @extend_schema(
        request=AppointmentCompleteSerializer,
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def complete(self, request: Request, pk: Any = None) -> Response:
        """POST /api/v1/appointments/{id}/complete/ — close a visit.

        Two actors, one command (DRF-1064 / owner decision OD-V1): the
        assigned specialist, and an administrator of the tenant the
        booking belongs to. Before this, the salon could not close a
        visit at all — and neither could the master of the pilot salon,
        who has no way to log in. That is why no booking in this system
        had ever reached ``completed``, and why everything hanging off
        completion (commission, payment capture, review request, RFM)
        had never run once.

        The state machine, row lock, event emission and capture
        scheduling below are reused verbatim; what changed is who is
        allowed to reach them and that the closure now records WHO.
        """
        serializer = AppointmentCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expected_version = serializer.validated_data.get('expected_version')

        if not may_operate_on_bookings(request):
            return error_response(
                "FORBIDDEN",
                "Only the assigned specialist or a salon administrator "
                "can mark appointments as complete.",
                status_code=403,
            )
        try:
            with transaction.atomic():
                # Fetch inside the atomic block with select_for_update so
                # two concurrent POSTs serialise here. Without the lock,
                # both would pass can_complete() (status still CONFIRMED
                # in both reads), both flip status, both emit with
                # distinct event_ids — bot-platform processes both,
                # double-completing the booking (adversarial review B3).
                #
                # We bypass get_queryset() (which filters by
                # specialist__user) because select_for_update is on the
                # raw Appointment manager. The explicit owner check on
                # the next line restores the scope guard — belt-and-
                # suspenders for B4.
                try:
                    appointment = (
                        Appointment.objects
                        # of=('self',) — lock ONLY the base table. AMD-019
                        # made Appointment.service nullable → select_related
                        # now emits an outer join on the nullable side,
                        # and bare FOR UPDATE is rejected by Postgres
                        # (same trap as billing.charges' nullable invoice).
                        .select_for_update(of=("self",))
                        .select_related('specialist', 'client', 'service')
                        .get(pk=pk)
                    )
                except Appointment.DoesNotExist:
                    return error_response(
                        "NOT_FOUND", "Appointment not found.", status_code=404,
                    )
                # Row-aware capacity check, replacing the old
                # "is it my row?" test. Distinct from the role gate
                # above — that asks whether the caller has any
                # operational standing at all; this asks in what
                # capacity they act on THIS booking, and returns the
                # OperationalActor value stamped on the row and the
                # event below. None → 404, never 403: answering
                # "forbidden" would confirm the id exists.
                actor = resolve_booking_operator(request, appointment)
                if actor is None:
                    return error_response(
                        "NOT_FOUND",
                        "Appointment not found.",
                        status_code=404,
                    )
                # Defence-in-depth #520: select_for_update bypasses
                # get_queryset's tenant filter. Explicit row-level
                # tenant assertion ensures a future refactor of
                # ownership semantics (multi-specialist, assistants,
                # salon-managed bookings) cannot silently re-open the
                # §6 gap. Legacy NULL rows allowed via fallback.
                request_tenant = getattr(request, "tenant", None)
                if (
                    request_tenant is not None
                    and appointment.tenant_id not in (None, request_tenant.id)
                ):
                    return error_response(
                        "NOT_FOUND",
                        "Appointment not found.",
                        status_code=404,
                    )

                # Optimistic concurrency (master MVP contract). Inside
                # the transaction, AFTER the lock, BEFORE any state
                # change: a stale caller must not complete, must not
                # emit, must not mutate. Reuses the existing
                # Appointment.version counter — no second concurrency
                # system. Version is NOT bumped by completion (it counts
                # reschedules); see AppointmentCompleteSerializer.
                if (
                    expected_version is not None
                    and appointment.version != expected_version
                ):
                    return error_response(
                        "STALE_VERSION",
                        f"Appointment {appointment.id} expected_version="
                        f"{expected_version} but current version is "
                        f"{appointment.version}.",
                        status_code=409,
                    )

                # State transition + event, shared verbatim with the
                # 3-hour sweep and the backlog command (see
                # application/services/completion.py) so a booking closed
                # by the front desk and one closed by the sweep are
                # indistinguishable to any consumer. complete() raises
                # ValidationError if the status is not CONFIRMED; under
                # the row lock that re-check sees the committed status of
                # any racing transaction.
                close_booking(appointment, completed_by=actor)
        except DjangoValidationError as e:
            return error_response(
                "INVALID_STATUS", str(e.message), status_code=422,
            )
        # D9 — schedule the two-stage capture for any held payment of
        # the just-completed appointment (pilot: immediate, delay 0).
        # Runs after the atomic block: the booking is durably completed
        # even if the broker/provider is down — reconciliation (and the
        # retry_capture command) covers the rest. No-op when the booking
        # has no held payment (no-prepayment path, D6).
        schedule_capture_safely(appointment)
        return success_response(AppointmentDetailSerializer(appointment).data)

    # -- No-show ------------------------------------------------------------

    @extend_schema(
        request=AppointmentCompleteSerializer,
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['post'], url_path='no-show')
    def no_show(self, request: Request, pk: Any = None) -> Response:
        """The specialist or a salon administrator marks the client as
        no-show. Transition confirmed → no_show. Distinct from cancel —
        preserves the "specialist held the slot" signal for revenue loss
        tracking + future reliability scoring (#511).

        Same two-actor model and the same ``expected_version`` contract
        as ``complete`` (DRF-1064): in a salon the person who sees that
        the client never arrived is usually at the front desk, not in
        the chair.
        """
        serializer = AppointmentCompleteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        expected_version = serializer.validated_data.get('expected_version')

        if not may_operate_on_bookings(request):
            return error_response(
                "FORBIDDEN",
                "Only the assigned specialist or a salon administrator "
                "can mark appointments as no-show.",
                status_code=403,
            )
        try:
            with transaction.atomic():
                # Same race-safe pattern as `complete`: select_for_update
                # + explicit owner check after the lock, atomic over the
                # state flip + envelope emit.
                try:
                    appointment = (
                        Appointment.objects
                        # of=('self',) — lock ONLY the base table. AMD-019
                        # made Appointment.service nullable → select_related
                        # now emits an outer join on the nullable side,
                        # and bare FOR UPDATE is rejected by Postgres
                        # (same trap as billing.charges' nullable invoice).
                        .select_for_update(of=("self",))
                        .select_related('specialist', 'client', 'service')
                        .get(pk=pk)
                    )
                except Appointment.DoesNotExist:
                    return error_response(
                        "NOT_FOUND", "Appointment not found.", status_code=404,
                    )
                # Same row-aware capacity check as `complete` — see that
                # action for why None becomes 404 rather than 403.
                actor = resolve_booking_operator(request, appointment)
                if actor is None:
                    return error_response(
                        "NOT_FOUND",
                        "Appointment not found.",
                        status_code=404,
                    )
                # Same defence-in-depth tenant assertion as `complete`
                # — select_for_update bypasses get_queryset's filter.
                request_tenant = getattr(request, "tenant", None)
                if (
                    request_tenant is not None
                    and appointment.tenant_id not in (None, request_tenant.id)
                ):
                    return error_response(
                        "NOT_FOUND",
                        "Appointment not found.",
                        status_code=404,
                    )
                # Optimistic concurrency — same placement rule as
                # `complete`: after the lock, before any state change.
                if (
                    expected_version is not None
                    and appointment.version != expected_version
                ):
                    return error_response(
                        "STALE_VERSION",
                        f"Appointment {appointment.id} expected_version="
                        f"{expected_version} but current version is "
                        f"{appointment.version}.",
                        status_code=409,
                    )
                appointment.mark_no_show(marked_by=actor)
                # Internal no-show signal — kept for in-process handlers
                # (#511 reliability scoring, revenue-loss tracking). This
                # topic is NOT in the cross-service taxonomy, so it stays
                # internal-delivery only; the bot mirror is fed by the
                # booking.cancelled emit below.
                emit_outbox_event(
                    topic=OutboxEvent.Topic.BOOKING_NO_SHOW,
                    data={
                        "appointment_id": str(appointment.id),
                        "client_id": str(appointment.client_id),
                        "specialist_id": str(appointment.specialist_id),
                        # Attribution counterpart of completed_by. The
                        # Domain Event Registry already reserves an
                        # optional `marked_by` on appointment.no_show;
                        # this is the value behind it. Internal topic —
                        # no cross-service versioning consequence.
                        "no_show_marked_by": actor,
                    },
                    # The affected user is the customer, not the operator
                    # — same §2.2 rule corrected in `complete` above. The
                    # booking.cancelled emit below already got this right.
                    user_id=appointment.client_id,
                    tenant_id=safe_tenant_id(
                        appointment, context="booking.no_show",
                    ),
                    actor=envelope_actor_for(actor),
                )
                # Cross-service representation of a no-show. The bot's
                # ingest taxonomy has no "booking.no_show" name (it would
                # dead-letter); event-contract.md §3.2 models a no-show
                # as booking.cancelled + reason_code="user_no_show". The
                # consumer flips its RemoteBookingProxy to cancelled and
                # cancels pending reminders — the correct mirror state for
                # a client who didn't show. user_id is the customer (whose
                # booking this is), not the specialist who marked it.
                tenant_id = safe_tenant_id(
                    appointment, context="booking.cancelled",
                )
                emit_outbox_event(
                    topic=OutboxEvent.Topic.BOOKING_CANCELLED,
                    data={
                        "appointment_id": str(appointment.id),
                        "specialist_id": str(appointment.specialist_id),
                        "start_at": appointment.start_datetime.isoformat(),
                        # §3.2 vocabulary {user, admin, master, system}.
                        # `admin` has been in that closed set since the
                        # contract was written and no Ayla call site
                        # produced it until now — a front-desk no-show is
                        # the first. The bot consumer already branches on
                        # it (§3.2 step 3: notify the customer when
                        # cancelled_by ∈ {admin, master, system}), so this
                        # needs no bot-side change.
                        "cancelled_by": cancelled_by_for(actor),
                        "reason_code": "user_no_show",
                        "cancelled_at": timezone.now().isoformat(),
                    },
                    user_id=appointment.client_id,
                    tenant_id=tenant_id,
                    actor=envelope_actor_for(actor),
                )
        except DjangoValidationError as e:
            return error_response(
                "INVALID_STATUS", str(e.message), status_code=422,
            )
        return success_response(AppointmentDetailSerializer(appointment).data)

    # -- Status (spec-compliant: PATCH /appointments/{id}/status/) ----------

    @extend_schema(
        request={
            "application/json": {
                "type": "object",
                "properties": {"status": {"type": "string", "enum": ["cancelled", "completed"]}},
                "required": ["status"],
            },
        },
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['patch'], url_path='status')
    def update_status(self, request: Request, pk: Any = None) -> Response:
        """
        PATCH /appointments/{id}/status/ — spec-compliant status update.
        Delegates to cancel or complete based on requested status.
        """
        status_value = request.data.get('status')
        if not status_value:
            return error_response(
                "VALIDATION_ERROR", "status field is required.", status_code=400,
            )

        if status_value == 'cancelled':
            return self.cancel(request, pk=pk)
        if status_value == 'completed':
            return self.complete(request, pk=pk)

        return error_response(
            "INVALID_STATUS_TRANSITION",
            f"Cannot transition to '{status_value}' via this endpoint.",
            status_code=400,
        )

    # -- Reschedule (via booking engine) ------------------------------------

    @extend_schema(
        request=AppointmentRescheduleSerializer,
        responses={200: AppointmentDetailSerializer},
    )
    @action(detail=True, methods=['post'])
    def reschedule(self, request: Request, pk: Any = None) -> Response:
        # X-Idempotency-Key replay protection (#512). Same shape as
        # cancel — see that comment for contract details.
        try:
            cached, idem_record = lookup_or_open_idempotency(
                request,
                operation_name="booking.reschedule",
                target_type="Appointment",
                target_id=str(pk),
            )
        except IdempotencyConflict as exc:
            return error_response(
                "IDEMPOTENCY_CONFLICT", str(exc), status_code=422,
            )
        except IdempotencyInFlight as exc:
            return error_response(
                "IDEMPOTENCY_IN_FLIGHT", str(exc), status_code=409,
            )
        if cached is not None:
            return Response(cached["payload"], status=cached["status"])

        response = self._reschedule_inner(request, pk)
        if idem_record is not None:
            record_response(idem_record, response.status_code, response.data)
        return response

    def _reschedule_inner(self, request: Request, pk: Any) -> Response:
        """Reschedule implementation, isolated so the outer
        ``reschedule`` can record_response on EVERY return path."""
        try:
            appointment = self.get_queryset().get(pk=pk)
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )

        serializer = AppointmentRescheduleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        request_tenant = getattr(request, "tenant", None)
        dto = RescheduleBookingDTO(
            booking_id=appointment.id,
            initiator_user_id=request.user.id,
            new_start_at=serializer.validated_data['new_start_datetime'],
            initiator_role=(
                "specialist" if request.user.is_specialist else "client"
            ),
            expected_version=serializer.validated_data.get('expected_version'),
            # Defense-in-depth tenant boundary (Wave 1) — mirrors the
            # post-lock tenant assertion in complete()/no_show(); those
            # comments explain why get_queryset's filter alone isn't
            # enough (select_for_update bypasses it).
            tenant_id=request_tenant.id if request_tenant else None,
            command_key=request.META.get('HTTP_X_IDEMPOTENCY_KEY') or None,
            basis="mobile_app",
        )

        try:
            result = self.reschedule_booking_service_class().execute(dto)
        except SlotNotAvailableError as e:
            return error_response(
                "SLOT_NOT_AVAILABLE", str(e), status_code=409,
            )
        except RescheduleNotAllowedError as e:
            return error_response(
                "RESCHEDULE_NOT_ALLOWED", str(e), status_code=422,
            )
        except InvalidStateTransitionError as e:
            return error_response(
                "INVALID_STATUS", str(e), status_code=422,
            )
        except BookingWindowError as e:
            # New failure mode (Wave 1) — the shared guard now also
            # checks min-ahead/horizon/grid-alignment on reschedule.
            # Caught locally (not left to the global exception handler)
            # so the idempotency wrapper's record_response still runs —
            # same status/code the global handler would produce anyway.
            return error_response(
                "BOOKING_WINDOW_INVALID", str(e), status_code=400,
            )
        except StaleVersionError as e:
            return error_response(
                "STALE_VERSION", str(e), status_code=409,
            )
        except AppointmentTerminalError as e:
            return error_response(
                "APPOINTMENT_TERMINAL", str(e), status_code=409,
            )
        except ExpectedVersionRequiredError as e:
            # Only reachable once settings.RESCHEDULE_MOBILE_UNVERSIONED_
            # ALLOWED is flipped to False (default True today — see that
            # setting's docstring).
            return error_response(
                "EXPECTED_VERSION_REQUIRED", str(e), status_code=400,
            )
        except TenantMismatchError:
            # Info-hiding — same rationale as complete()/no_show(): don't
            # reveal that a booking exists in another tenant.
            return error_response(
                "NOT_FOUND", "Appointment not found.", status_code=404,
            )

        appointment.refresh_from_db()
        data = AppointmentDetailSerializer(appointment).data
        # revision_id has no FK on Appointment (it's the audit row this
        # command just created) — echoed from the service result rather
        # than the serializer. version above already comes from the
        # refreshed model field.
        data['revision_id'] = str(result.revision_id)
        return success_response(data)
