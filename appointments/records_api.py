"""Records endpoints for W1 customer-records-flow (task #97).

Three endpoints, all under ``/api/v1/internal/me/bookings/``,
authenticated via ``IsBotServiceWithVerifiedClient`` (Bearer +
X-External-User-ID). The bot acts on behalf of the customer; no
client JWT exists in bot-platform per memory
``project_identity_bridging_pattern``.

- ``GET  /api/v1/internal/me/bookings/?section=upcoming|history``
    Paginated list of the customer's bookings ACROSS all their
    tenants (multi-tenant customer per #246). Section gate:
    ``upcoming`` = active statuses with start_datetime ≥ now,
    ``history`` = terminal statuses OR start_datetime < now.
    Cursor pagination (limit=20 default, max=100).

- ``GET  /api/v1/internal/me/bookings/{booking_id}/``
    Full detail of a single booking. Cross-tenant: filter by
    ``appointment.client == resolved_user`` only — no tenant scope.

- ``POST /api/v1/internal/me/bookings/{booking_id}/repeat-intent/``
    Returns prefill data the bot's "Записаться ещё" CTA uses:
    ``service_id``, ``specialist_id``, ``last_price``, suggested
    slots (slot suggestions are out of MVP scope — empty list for
    now; the bot falls back to opening catalog with master filter).
    ``service_id`` resolves the AMD-019 XOR (marketplace ``service``
    vs salon ``salon_service``) exactly like the list/detail items do;
    an unresolvable reference is a 422, never a ``"None"`` string
    (DRF-1049).

Status field shape: every booking carries a ``derived_status`` per
the canonical taxonomy in ``appointments/records_status.py``. UI
badges key off that string.
"""
from __future__ import annotations

import logging
from uuid import UUID

from django.db.models import Q
from django.utils import timezone
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.models import Appointment
from appointments.records_status import DERIVED_STATUSES, derive_status
from users.permissions import IsBotServiceWithVerifiedClient
from users.response import error_response, success_response


logger = logging.getLogger(__name__)


# Booking states that count as "upcoming" — anything non-terminal AND
# still in the future.
_UPCOMING_STATUSES = (
    Appointment.Status.PENDING,
    Appointment.Status.AWAITING_PAYMENT,
    Appointment.Status.CONFIRMED,
)


# Pagination caps. Default 20 keeps the response under typical mobile
# 4G payload budgets; max 100 is a safety against accidental large
# pulls from the bot side.
DEFAULT_LIMIT = 20
MAX_LIMIT = 100


# ---------------------------------------------------------------------------
# Response serializers
# ---------------------------------------------------------------------------


class _SpecialistShortSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    display_name = serializers.CharField()


class _ServiceShortSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    name = serializers.CharField()
    duration_minutes = serializers.IntegerField()


class _TenantShortSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    slug = serializers.CharField()
    name = serializers.CharField()


class _PaymentShortSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    amount = serializers.DecimalField(max_digits=10, decimal_places=2)
    refunded_amount = serializers.DecimalField(
        max_digits=10, decimal_places=2,
    )
    status = serializers.CharField()


class BookingListItemSerializer(serializers.Serializer):
    """List item shape — minimal payload for /bookings/ list.

    Excludes notes, snapshot fields, and full payment list to keep
    page payload small. UI fetches detail via the singleton endpoint
    when the customer taps a row.
    """
    id = serializers.UUIDField()
    status = serializers.CharField()
    derived_status = serializers.ChoiceField(choices=DERIVED_STATUSES)
    start_datetime = serializers.DateTimeField()
    end_datetime = serializers.DateTimeField()
    price = serializers.DecimalField(max_digits=10, decimal_places=2)
    service = _ServiceShortSerializer()
    specialist = _SpecialistShortSerializer()
    tenant = _TenantShortSerializer()
    is_first_visit = serializers.BooleanField()
    original_datetime = serializers.DateTimeField(allow_null=True)


class BookingDetailSerializer(BookingListItemSerializer):
    """Full detail — adds notes, payments, cancellation context."""
    notes = serializers.CharField(allow_blank=True)
    cancellation_reason = serializers.CharField(allow_blank=True)
    cancelled_by_client = serializers.BooleanField()
    payments = _PaymentShortSerializer(many=True)


class _RepeatIntentResponseSerializer(serializers.Serializer):
    service_id = serializers.UUIDField()
    specialist_id = serializers.UUIDField()
    last_price = serializers.DecimalField(max_digits=10, decimal_places=2)
    # MVP: empty list. Slot suggestions deferred — bot opens catalog
    # with master filter as fallback per Tau's customer-records-flow.md
    # §R6 fallback path.
    suggested_slots = serializers.ListField(
        child=serializers.DateTimeField(),
        allow_empty=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_service_id(appointment) -> str | None:
    """Return the id of whichever typed service reference is filled.

    AMD-019 option A made the reference a XOR: a booking carries either
    the marketplace ``service`` or the salon-catalog ``salon_service``,
    never both (CHECK ``appointment_exactly_one_service_source``, see
    ``appointments/models.py``). Bot-created salon bookings have
    ``service_id IS NULL``, so any reader that touches ``service_id``
    directly renders the literal string ``"None"`` (DRF-1049).

    Returns ``None`` when neither side is set — only reachable for rows
    predating the constraint. Callers MUST treat that as an error, never
    as a value.
    """
    service_id = appointment.service_id or appointment.salon_service_id
    return str(service_id) if service_id else None


def _build_item(appointment) -> dict:
    """Build the list-item dict for one appointment.

    Caller is expected to have prefetched ``payments``, ``specialist``,
    ``service``, ``tenant`` to avoid N+1 in the list endpoint.
    """
    payments = list(appointment.payments.all())
    return {
        "id": str(appointment.id),
        "status": appointment.status,
        "derived_status": derive_status(appointment, payments=payments),
        "start_datetime": appointment.start_datetime,
        "end_datetime": appointment.end_datetime,
        "price": appointment.price,
        "service": {
            # AMD-019 option A: the typed reference is XOR — marketplace
            # ``service_id`` or ``salon_service_id``. Display fields come
            # from the booking-time SNAPSHOTS (the historical source),
            # so salon bookings read identically to marketplace ones.
            "id": str(appointment.service_id or appointment.salon_service_id),
            "name": appointment.snapshot_service_name,
            "duration_minutes": appointment.snapshot_duration_minutes,
        },
        "specialist": {
            "id": str(appointment.specialist_id),
            "display_name": appointment.specialist.display_name,
        },
        "tenant": {
            "id": str(appointment.tenant_id),
            "slug": appointment.tenant.slug,
            "name": appointment.tenant.name,
        },
        "is_first_visit": appointment.is_first_visit,
        # Reschedule history is not materialised on the model yet
        # (would require either an AppointmentHistory row or a
        # `previous_start_datetime` field). Surface as null in MVP
        # so the contract is set when we add the backing data.
        "original_datetime": None,
    }


def _build_detail(appointment) -> dict:
    base = _build_item(appointment)
    payments = list(appointment.payments.all().order_by("-created_at"))
    base.update({
        "notes": appointment.notes or "",
        "cancellation_reason": appointment.cancellation_reason or "",
        "cancelled_by_client": (
            getattr(appointment, "cancelled_by_id", None)
            == appointment.client_id
        ),
        "payments": [
            {
                "id": str(p.id),
                "amount": p.amount,
                "refunded_amount": p.refunded_amount,
                "status": p.status,
            }
            for p in payments
        ],
    })
    return base


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


CURSOR_SEPARATOR = "|"


class _CursorPageMixin:
    """Tiny cursor-pagination helper.

    Cursors are ``"<start_datetime ISO8601>|<appointment id>"``. For
    'upcoming' the sort is ascending; for 'history', descending. The
    cursor points to the LAST item returned — the next page starts
    strictly after it *in sort order*.

    DRF-1053 — why the id half exists. The sort key is the PAIR
    ``(start_datetime, id)``, so the cursor filter has to compare the
    same pair. The original filter compared ``start_datetime`` alone;
    when a page boundary fell inside a group of bookings sharing one
    timestamp, every remaining row of that group was excluded by
    ``start_datetime__gt`` / ``__lt`` and never came back. The caller
    saw a shorter list, not an error — a silent loss.

    The old docstring claimed ``start_datetime`` is unique "because the
    booking engine prevents double-booking". It does not: the guard is
    per specialist, while this list spans every specialist and every
    tenant the customer ever booked with. Two masters at the same
    o'clock is ordinary, and a bulk import stamps whole batches
    identically.

    Legacy cursors carrying only the timestamp (issued before this fix
    and possibly still in flight from the bot across a deploy) are
    still accepted and fall back to the old timestamp-only comparison —
    a stale cursor degrades to the previous behaviour instead of 400-ing
    mid-scroll.
    """

    def _paginate(self, queryset, *, limit: int, cursor: str | None,
                  ascending: bool):
        order = "start_datetime" if ascending else "-start_datetime"
        # Secondary key on id makes the sort a TOTAL order: bookings
        # sharing a timestamp still each have one defined position.
        # The cursor filter below must mirror this exact pair.
        qs = queryset.order_by(order, "id" if ascending else "-id")

        if cursor:
            cursor_dt, cursor_id, err = self._decode_cursor(cursor)
            if err is not None:
                return None, None, err
            if cursor_id is None:
                # Legacy timestamp-only cursor — old semantics.
                lookup = (
                    "start_datetime__gt" if ascending
                    else "start_datetime__lt"
                )
                qs = qs.filter(**{lookup: cursor_dt})
            elif ascending:
                qs = qs.filter(
                    Q(start_datetime__gt=cursor_dt)
                    | Q(start_datetime=cursor_dt, id__gt=cursor_id),
                )
            else:
                qs = qs.filter(
                    Q(start_datetime__lt=cursor_dt)
                    | Q(start_datetime=cursor_dt, id__lt=cursor_id),
                )

        page = list(qs[: limit + 1])
        has_more = len(page) > limit
        page = page[:limit]
        next_cursor = (
            self._encode_cursor(page[-1])
            if has_more and page else None
        )
        return page, next_cursor, None

    @staticmethod
    def _encode_cursor(appointment) -> str:
        """Serialise the full sort key of the last row on the page."""
        return (
            f"{appointment.start_datetime.isoformat()}"
            f"{CURSOR_SEPARATOR}{appointment.id}"
        )

    @staticmethod
    def _decode_cursor(raw: str):
        """``raw`` -> ``(datetime, uuid | None, error | None)``.

        ISO8601 never contains ``|``, so partitioning on it is
        unambiguous. A missing id half means a legacy cursor, not an
        error.
        """
        from django.utils.dateparse import parse_datetime

        dt_part, separator, id_part = raw.partition(CURSOR_SEPARATOR)
        try:
            cursor_dt = parse_datetime(dt_part)
        except (ValueError, TypeError):
            return None, None, "INVALID_CURSOR"
        if cursor_dt is None:
            return None, None, "INVALID_CURSOR"
        if not separator:
            return cursor_dt, None, None
        try:
            cursor_id = UUID(id_part)
        except (ValueError, TypeError, AttributeError):
            return None, None, "INVALID_CURSOR"
        return cursor_dt, cursor_id, None


class MeBookingsListView(_CursorPageMixin, APIView):
    """GET /api/v1/internal/me/bookings/?section=upcoming|history"""
    # JWT auth is OFF — bot is the caller, identity comes from
    # X-External-User-ID. See PR #158 (#85) for the pattern + tests.
    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]
    serializer_class = BookingListItemSerializer

    @extend_schema(
        # Phase 5 — explicit operation_id resolves the drf-spectacular
        # collision with MeBookingDetailView (both GET under the same
        # /api/v1/internal/me/bookings/ prefix). Stable name pinned
        # here so mobile / bot codegen does not re-bind to numeral
        # suffixes when the route order shifts.
        operation_id="internal_me_bookings_list",
        tags=["internal"],
        parameters=[],
        responses={
            200: inline_serializer(
                name="MeBookingsListResponse",
                fields={
                    "data": inline_serializer(
                        name="MeBookingsListData",
                        fields={
                            "items": BookingListItemSerializer(many=True),
                            "next_cursor": serializers.CharField(
                                allow_null=True,
                            ),
                        },
                    ),
                },
            ),
            400: OpenApiResponse(description="Bad section / cursor"),
            403: OpenApiResponse(description="Bearer / external id invalid"),
        },
    )
    def get(self, request: Request) -> Response:
        section = request.query_params.get("section", "upcoming")
        if section not in ("upcoming", "history"):
            return error_response(
                "VALIDATION_ERROR",
                "section must be one of: upcoming, history",
            )

        try:
            limit = int(request.query_params.get("limit", DEFAULT_LIMIT))
        except ValueError:
            return error_response(
                "VALIDATION_ERROR", "limit must be an integer",
            )
        limit = max(1, min(limit, MAX_LIMIT))

        cursor = request.query_params.get("cursor")

        # Multi-tenant: NO tenant filter — customer sees their bookings
        # across all tenants they've ever booked in (per #246
        # multi-provider customer model). Tenant scope lives in the
        # serializer's per-item `tenant` field for the UI to group by.
        base_qs = (
            Appointment.objects
            .filter(client=request.user)
            .select_related(
                "specialist", "service", "service__category", "tenant",
            )
            .prefetch_related("payments")
        )
        now = timezone.now()
        if section == "upcoming":
            qs = base_qs.filter(
                status__in=[s.value for s in _UPCOMING_STATUSES],
                start_datetime__gte=now,
            )
            ascending = True
        else:  # history
            terminal = list(Appointment.TERMINAL_STATUSES)
            qs = base_qs.filter(
                Q(status__in=[s.value for s in terminal])
                | Q(start_datetime__lt=now),
            )
            ascending = False

        page, next_cursor, err = self._paginate(
            qs, limit=limit, cursor=cursor, ascending=ascending,
        )
        if err == "INVALID_CURSOR":
            return error_response("VALIDATION_ERROR", "cursor invalid")

        items = [_build_item(a) for a in page]
        return success_response({
            "items": items,
            "next_cursor": next_cursor,
        })


class MeBookingDetailView(APIView):
    """GET /api/v1/internal/me/bookings/{booking_id}/"""
    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]
    serializer_class = BookingDetailSerializer

    @extend_schema(
        # Phase 5 — explicit operation_id to differentiate from
        # MeBookingsListView's GET on the same URL prefix.
        operation_id="internal_me_bookings_retrieve",
        tags=["internal"],
        responses={
            200: BookingDetailSerializer,
            403: OpenApiResponse(description="Auth invalid"),
            404: OpenApiResponse(
                description=(
                    "Booking not found for this customer (info-hidden "
                    "— same response whether the booking is on another "
                    "customer or simply doesn't exist)"
                ),
            ),
        },
    )
    def get(self, request: Request, booking_id: UUID) -> Response:
        try:
            appointment = (
                Appointment.objects
                .filter(client=request.user)
                .select_related(
                    "specialist", "service", "service__category", "tenant",
                )
                .prefetch_related("payments")
                .get(pk=booking_id)
            )
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Запись не найдена.", status_code=404,
            )
        return success_response(_build_detail(appointment))


class MeBookingRepeatIntentView(APIView):
    """POST /api/v1/internal/me/bookings/{booking_id}/repeat-intent/

    Returns the prefill data the bot's «Записаться ещё» CTA uses.
    MVP: surfaces service_id + specialist_id + last_price. Suggested
    slots are deferred — the bot falls back to opening the catalog
    with a master filter per Tau's customer-records-flow.md §R6.

    ``service_id`` follows the same XOR resolution as the list/detail
    endpoints (``_build_item``): marketplace ``service`` OR salon
    ``salon_service``, whichever the booking actually carries. Reading
    ``service_id`` directly used to emit the literal string ``"None"``
    with HTTP 200 for every salon booking — i.e. for 100% of the
    bookings the bot creates (DRF-1049). When neither reference
    resolves the endpoint now fails loudly (422) instead of handing
    the caller an unusable value.
    """
    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]
    serializer_class = _RepeatIntentResponseSerializer

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: _RepeatIntentResponseSerializer,
            404: OpenApiResponse(description="Booking not found"),
            422: OpenApiResponse(
                description=(
                    "Booking carries no resolvable service reference "
                    "(neither `service` nor `salon_service`) — prefill "
                    "cannot be built"
                ),
            ),
        },
    )
    def post(self, request: Request, booking_id: UUID) -> Response:
        try:
            appointment = (
                Appointment.objects
                .filter(client=request.user)
                .select_related("specialist", "service", "salon_service")
                .get(pk=booking_id)
            )
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Запись не найдена.", status_code=404,
            )

        service_id = _resolve_service_id(appointment)
        if service_id is None:
            # Unreachable for rows created under the XOR CHECK; a hit
            # here means legacy/corrupt data. Surface it as an explicit
            # failure so the bot can branch, and log so it is findable.
            logger.error(
                "repeat_intent.unresolved_service booking_id=%s "
                "service_id=None salon_service_id=None",
                appointment.id,
            )
            return error_response(
                "SERVICE_NOT_FOUND",
                "У записи не указана услуга — повтор невозможен.",
                status_code=422,
            )

        return success_response({
            "service_id": service_id,
            "specialist_id": str(appointment.specialist_id),
            "last_price": appointment.price,
            # Slot suggestions deferred — see class docstring.
            "suggested_slots": [],
        })
