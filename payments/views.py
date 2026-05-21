"""Payments API views — YooKassa integration."""
from __future__ import annotations

import ipaddress
import logging
from uuid import uuid4

from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer
from rest_framework import permissions, serializers as drf_serializers
from rest_framework.exceptions import PermissionDenied
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from appointments.infrastructure.outbox import emit_outbox_event, safe_tenant_id
from appointments.models import Appointment, OutboxEvent
from payments.models import Payment
from users.permissions import IsClient
from users.response import error_response, success_response

from .exceptions import PaymentClientError, PaymentConfigError
from .serializers import PaymentCreateSerializer, PaymentDetailSerializer, PaymentRefundSerializer
from .services import YooKassaService, build_appointment_receipt

logger = logging.getLogger(__name__)


def _get_yookassa() -> YooKassaService:
    return YooKassaService()


def _verify_basic_auth(request: Request) -> bool:
    """Verify the Authorization: Basic header against the configured creds.

    Returns True when:
    - either env var (user OR pass) is empty (no auth configured — skip), OR
    - the header is present AND base64-decoded creds match the env values

    Returns False when creds are configured but the header is missing or
    doesn't match. Constant-time comparison via secrets.compare_digest to
    avoid the timing side-channel that ``==`` on user-controlled strings
    introduces.
    """
    import base64
    import secrets

    expected_user = getattr(settings, "YOOKASSA_WEBHOOK_BASIC_AUTH_USER", "")
    expected_pass = getattr(settings, "YOOKASSA_WEBHOOK_BASIC_AUTH_PASS", "")
    if not expected_user or not expected_pass:
        return True

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth_header[len("Basic "):]).decode("utf-8")
        user, _, password = decoded.partition(":")
    except (ValueError, UnicodeDecodeError):
        return False

    user_ok = secrets.compare_digest(user, expected_user)
    pass_ok = secrets.compare_digest(password, expected_pass)
    return user_ok and pass_ok


def _client_ip(request: Request) -> str:
    """Return the IP of the outermost trusted proxy's source.

    Standard nginx config (``proxy_set_header X-Forwarded-For
    $proxy_add_x_forwarded_for;``) APPENDS one entry on each pass — the
    IP that proxy received the request from. So with N trusted proxies
    in front of Django, the real client lives at index ``-N`` in XFF;
    everything before it is *client-controlled and untrusted*. Reading
    ``xff[0]`` (the leftmost entry) is the classic spoofing bypass:
    attacker sets ``X-Forwarded-For: <victim_ip>``, our proxy appends
    its own view, and the leftmost entry is the attacker's lie.

    Falls back to ``REMOTE_ADDR`` (Django's TCP source) when XFF is
    missing or shorter than ``YOOKASSA_WEBHOOK_TRUSTED_PROXY_COUNT`` —
    typical when Django is reached directly without a proxy in front.
    """
    from django.conf import settings

    trusted = max(1, getattr(settings, "YOOKASSA_WEBHOOK_TRUSTED_PROXY_COUNT", 1))
    xff_raw = request.META.get("HTTP_X_FORWARDED_FOR", "")
    xff = [s.strip() for s in xff_raw.split(",") if s.strip()]
    if len(xff) >= trusted:
        return xff[-trusted]
    return request.META.get("REMOTE_ADDR", "")


def _ip_in_allowlist(ip: str, allowlist: list[str]) -> bool:
    """Return True if *ip* matches any CIDR / single-IP entry in *allowlist*.

    An empty allowlist is treated as 'not configured' and returns True —
    the caller decides whether that means permit (dev) or fail closed (prod).
    Malformed entries are logged and skipped so one bad line in the env var
    does not nuke webhook processing.
    """
    if not allowlist:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowlist:
        try:
            if addr in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            logger.warning(
                "YOOKASSA_WEBHOOK_ALLOWED_IPS has invalid entry: %r", entry,
            )
    return False


class PaymentCreateView(APIView):
    """
    POST /api/v1/payments/create

    Client creates a payment for their appointment.
    Returns confirmation_url to redirect the user to YooKassa checkout.
    Two-stage: payment is held (not captured) until appointment is completed.
    """
    permission_classes = [permissions.IsAuthenticated, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payment'
    serializer_class = PaymentCreateSerializer

    @extend_schema(
        request=PaymentCreateSerializer,
        responses={
            200: inline_serializer(
                name="PaymentCreateExistingResponse",
                fields={
                    "payment_id": drf_serializers.UUIDField(),
                    "confirmation_url": drf_serializers.URLField(),
                    "amount": drf_serializers.FloatField(),
                },
            ),
            201: inline_serializer(
                name="PaymentCreateResponse",
                fields={
                    "payment_id": drf_serializers.UUIDField(),
                    "confirmation_url": drf_serializers.URLField(),
                    "amount": drf_serializers.FloatField(),
                },
            ),
            404: OpenApiResponse(description="Appointment not found"),
            422: OpenApiResponse(description="Invalid appointment status"),
            502: OpenApiResponse(description="Payment provider error"),
            503: OpenApiResponse(description="Payment provider not configured"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = PaymentCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        appointment_id = serializer.validated_data['appointment_id']
        return_url = serializer.validated_data['return_url']

        # Fetch appointment owned by this client
        try:
            appointment = (
                Appointment.objects
                .select_related('specialist', 'service')
                .get(id=appointment_id, client=request.user)
            )
        except Appointment.DoesNotExist:
            return error_response('NOT_FOUND', 'Appointment not found.', status_code=404)

        # Only pending / awaiting_payment appointments can be paid
        if appointment.status not in (
            Appointment.Status.PENDING,
            Appointment.Status.AWAITING_PAYMENT,
        ):
            return error_response(
                'INVALID_STATUS',
                f'Cannot pay for appointment in status "{appointment.status}".',
                status_code=422,
            )

        # Idempotency: return existing pending payment if one exists
        existing = appointment.payments.filter(
            status=Payment.Status.PENDING,
        ).order_by('-created_at').first()
        if existing and existing.provider_payment_id:
            return success_response(
                {
                    'payment_id': str(existing.id),
                    'confirmation_url': existing.provider_client_secret,
                    'amount': float(existing.amount),
                },
                status_code=200,
            )

        idempotency_key = request.META.get('HTTP_X_IDEMPOTENCY_KEY', str(uuid4()))
        description = (
            f'BeautyGO: {appointment.service.name} у {appointment.specialist.display_name}'
        )

        # 54-ФЗ receipt — mandatory for production payments in RF.
        # Always built; the YooKassaService skips the field only when
        # caller passes None (never the case here).
        receipt = build_appointment_receipt(appointment, appointment.price)

        try:
            svc = _get_yookassa()
            result = svc.create_payment(
                amount=appointment.price,
                appointment_id=appointment.id,
                description=description,
                return_url=return_url,
                idempotency_key=idempotency_key,
                capture=False,  # two-stage: hold first
                receipt=receipt,
            )
        except PaymentConfigError as exc:
            logger.error('YooKassa not configured: %s', exc)
            return error_response(
                'SERVICE_UNAVAILABLE',
                'Payment provider not configured.',
                status_code=503,
            )
        except PaymentClientError as exc:
            logger.error('YooKassa create_payment failed: %s', exc)
            return error_response(
                'PAYMENT_PROVIDER_ERROR',
                'Payment provider error. Please try again.',
                status_code=502,
            )

        with transaction.atomic():
            payment = Payment.objects.create(
                appointment=appointment,
                amount=appointment.price,
                status=Payment.Status.PENDING,
                specialist_income=result['specialist_income'],
                platform_fee=result['platform_fee'],
                provider='yookassa',
                provider_payment_id=result['provider_payment_id'],
                provider_client_secret=result['confirmation_url'],
            )
            # Move appointment to awaiting_payment
            if appointment.status == Appointment.Status.PENDING:
                appointment.status = Appointment.Status.AWAITING_PAYMENT
                appointment.save(update_fields=['status'])

        logger.info(
            'Payment created: payment_id=%s appointment_id=%s amount=%s',
            payment.id, appointment.id, payment.amount,
        )

        return success_response(
            {
                'payment_id': str(payment.id),
                'confirmation_url': result['confirmation_url'],
                'amount': float(payment.amount),
            },
            status_code=201,
        )


class PaymentDetailView(APIView):
    """GET /api/v1/payments/{id} — payment status."""
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = PaymentDetailSerializer

    @extend_schema(
        responses={
            200: PaymentDetailSerializer,
            404: OpenApiResponse(description="Payment not found"),
        },
    )
    def get(self, request: Request, pk) -> Response:
        try:
            payment = (
                Payment.objects
                .select_related('appointment__client', 'appointment__specialist')
                .get(id=pk)
            )
        except Payment.DoesNotExist:
            return error_response('NOT_FOUND', 'Payment not found.', status_code=404)

        # Only the appointment's client or the specialist can view
        appt = payment.appointment
        user = request.user
        if not (
            appt.client_id == user.id
            or (user.is_specialist and appt.specialist.user_id == user.id)
        ):
            raise PermissionDenied('Access denied.')

        return success_response(PaymentDetailSerializer(payment).data)


class PaymentWebhookView(APIView):
    """
    POST /api/v1/payments/webhook

    YooKassa webhook receiver. Idempotent — re-delivery of the same event
    is detected via last_webhook_event_id.

    Verification strategy (defense-in-depth):
    1. Source IP allowlist (``YOOKASSA_WEBHOOK_ALLOWED_IPS`` env). YooKassa
       does not HMAC-sign webhooks; it publishes its source IP ranges and
       expects integrators to allowlist them. Unknown sources get 403 — an
       attacker who can't spoof YooKassa's IP can't trigger state changes
       even with a forged payload.
    2. Re-fetch payment from YooKassa API rather than trusting the
       webhook payload (already in place below).
    3. Idempotency key ``last_webhook_event_id`` guards against replay.
    4. Scoped throttle caps amplification at 100 req/min from any single
       source — bounds YooKassa API fan-out in a storm.

    When ``YOOKASSA_WEBHOOK_ALLOWED_IPS`` is unset (dev / initial deploy)
    the view logs a warning once and accepts all requests.
    """
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'webhook_payment'

    @extend_schema(
        tags=["webhook"],
        request=inline_serializer(
            name="YooKassaWebhookEvent",
            fields={
                "event": drf_serializers.CharField(),
                "object": drf_serializers.DictField(),
            },
        ),
        responses={
            200: inline_serializer(
                name="YooKassaWebhookAck",
                fields={"status": drf_serializers.CharField()},
            ),
        },
    )
    def post(self, request: Request) -> Response:
        allowlist = getattr(settings, "YOOKASSA_WEBHOOK_ALLOWED_IPS", [])
        client_ip = _client_ip(request)
        if not allowlist:
            logger.warning(
                "YOOKASSA_WEBHOOK_ALLOWED_IPS unset — webhook accepts all "
                "sources (client_ip=%s). Set the env for defence in depth.",
                client_ip,
            )
        elif not _ip_in_allowlist(client_ip, allowlist):
            logger.warning(
                "Rejected YooKassa webhook from unexpected source %s",
                client_ip,
            )
            raise PermissionDenied("Source IP not allowed.")

        # Basic Auth — second layer on top of the IP allowlist. YooKassa
        # supports `https://user:pass@host/...` URLs in their webhook
        # config; we verify here. When the env is unset we skip silently
        # (dev mode); prod.py enforces both env vars present.
        if not _verify_basic_auth(request):
            logger.warning(
                "Rejected YooKassa webhook — invalid Basic Auth credentials"
            )
            raise PermissionDenied("Invalid credentials.")

        event = request.data.get('event')
        obj = request.data.get('object', {})
        provider_payment_id = obj.get('id', '')
        event_id = request.META.get('HTTP_X_REQUEST_ID', f'{event}:{provider_payment_id}')

        if not event or not provider_payment_id:
            return Response({'status': 'ignored'}, status=200)

        try:
            payment = Payment.objects.select_related(
                'appointment',
            ).get(provider_payment_id=provider_payment_id)
        except Payment.DoesNotExist:
            # Unknown payment — could be a test notification; acknowledge silently
            return Response({'status': 'ok'}, status=200)

        # Idempotency: skip if we already processed this event
        if payment.last_webhook_event_id == event_id:
            return Response({'status': 'duplicate'}, status=200)

        # Verify payment state against YooKassa (do not trust payload alone)
        try:
            svc = _get_yookassa()
            info = svc.get_payment_info(provider_payment_id)
        except (PaymentClientError, PaymentConfigError) as exc:
            logger.error('YooKassa get_payment_info failed: %s', exc)
            # Return 200 to stop YooKassa retries; we'll reconcile later
            return Response({'status': 'ok'}, status=200)

        yookassa_status = info['status']

        with transaction.atomic():
            # Re-fetch under row lock so two concurrent webhooks for the
            # same payment serialise here. Without this, both would pass
            # the line 384 idempotency check, race through the branches,
            # and emit twice with distinct event_ids → bot-platform sees
            # two state transitions (adversarial review B3, ai-bot-platform#509).
            payment = (
                Payment.objects
                .select_for_update()
                .select_related('appointment')
                .get(pk=payment.pk)
            )
            # Re-check idempotency inside the lock — same key under
            # serial execution means the prior delivery already won.
            if payment.last_webhook_event_id == event_id:
                return Response({'status': 'duplicate'}, status=200)

            # Phase 1: state mutations per event branch. No emits here
            # — defer all emits to AFTER payment.save() so the
            # OutboxEvent.created_at is causally after the row commit
            # (adversarial review N5).
            refunded_val = None  # carried across phases for refund branch.

            if event == 'payment.waiting_for_capture' and yookassa_status == 'waiting_for_capture':
                payment.status = Payment.Status.AUTHORIZED
                payment.appointment.status = Appointment.Status.CONFIRMED
                payment.appointment.save(update_fields=['status'])

            elif event == 'payment.succeeded' and yookassa_status == 'succeeded':
                payment.status = Payment.Status.PAID

            elif event == 'payment.canceled' and yookassa_status == 'canceled':
                payment.status = Payment.Status.FAILED
                if payment.appointment.status not in Appointment.TERMINAL_STATUSES:
                    payment.appointment.status = Appointment.Status.CANCELLED
                    payment.appointment.save(update_fields=['status'])

            elif event == 'refund.succeeded':
                refunded_val = info.get('refunded_amount', payment.amount)
                payment.refunded_amount = refunded_val
                if refunded_val >= payment.amount:
                    payment.status = Payment.Status.REFUNDED
                else:
                    payment.status = Payment.Status.PARTIALLY_REFUNDED

            payment.last_webhook_event_id = event_id
            payment.save()

            # Phase 2: emits with explicit topic constants so the
            # AST-walking coverage test in
            # appointments/tests/test_state_emit_coverage_509.py can
            # statically verify every topic has an emit site. Same
            # `event` branching as Phase 1, deliberately verbose to
            # keep the topic visible to AST.
            tenant_id = safe_tenant_id(
                payment.appointment, context="payments.webhook",
            )
            client_id = payment.appointment.client_id

            if event == 'payment.waiting_for_capture' and yookassa_status == 'waiting_for_capture':
                # booking.confirmed — payment hold is the only path in
                # this codebase that moves an appointment to CONFIRMED.
                # Without this emit, bot-platform memory drifts ("your
                # booking is awaiting payment" days after hold landed).
                emit_outbox_event(
                    topic=OutboxEvent.Topic.BOOKING_CONFIRMED,
                    data={
                        'booking_id': str(payment.appointment_id),
                        'client_id': str(client_id),
                        'specialist_id': str(payment.appointment.specialist_id),
                        'payment_id': str(payment.id),
                    },
                    user_id=client_id,
                    tenant_id=tenant_id,
                    actor="system",
                )
            elif event == 'payment.succeeded' and yookassa_status == 'succeeded':
                emit_outbox_event(
                    topic=OutboxEvent.Topic.PAYMENT_CONFIRMED,
                    data={
                        'payment_id': str(payment.id),
                        'appointment_id': str(payment.appointment_id),
                        'amount': str(payment.amount),
                    },
                    user_id=client_id,
                    tenant_id=tenant_id,
                    actor="system",
                )
            elif event == 'refund.succeeded':
                emit_outbox_event(
                    topic=OutboxEvent.Topic.PAYMENT_REFUNDED,
                    data={
                        'payment_id': str(payment.id),
                        'appointment_id': str(payment.appointment_id),
                        'amount': str(refunded_val),
                        'is_partial': refunded_val < payment.amount,
                    },
                    user_id=client_id,
                    tenant_id=tenant_id,
                    actor="system",
                )

        logger.info('Webhook processed: event=%s payment=%s', event, payment.id)
        return Response({'status': 'ok'}, status=200)


class PaymentRefundView(APIView):
    """
    POST /api/v1/payments/{id}/refund

    Issue a full or partial refund. The appointment must be cancelled first,
    or the specialist/admin initiates it. For MVP: only the appointment's
    client can request a refund.

    Scoped on the same ``payment`` throttle bucket (5/min) as PaymentCreate —
    refund floods hit YooKassa API quotas and the provider bill the same way
    a create-payment flood does.
    """
    permission_classes = [permissions.IsAuthenticated, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'payment'
    serializer_class = PaymentRefundSerializer

    @extend_schema(
        request=PaymentRefundSerializer,
        responses={
            200: PaymentDetailSerializer,
            404: OpenApiResponse(description="Payment not found"),
            422: OpenApiResponse(description="Refund not allowed or amount exceeds paid"),
            502: OpenApiResponse(description="Payment provider error"),
            503: OpenApiResponse(description="Payment provider not configured"),
        },
    )
    def post(self, request: Request, pk) -> Response:
        try:
            payment = Payment.objects.select_related('appointment').get(id=pk)
        except Payment.DoesNotExist:
            return error_response('NOT_FOUND', 'Payment not found.', status_code=404)

        if payment.appointment.client_id != request.user.id:
            raise PermissionDenied('Access denied.')

        if payment.status not in (Payment.Status.AUTHORIZED, Payment.Status.PAID):
            return error_response(
                'REFUND_NOT_ALLOWED',
                f'Cannot refund payment in status "{payment.status}".',
                status_code=422,
            )

        serializer = PaymentRefundSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        refund_amount = serializer.validated_data.get('amount') or payment.net_amount
        if refund_amount > payment.net_amount:
            return error_response(
                'REFUND_AMOUNT_EXCEEDS_PAID',
                'Refund amount exceeds the net paid amount.',
                status_code=422,
            )

        idempotency_key = request.META.get('HTTP_X_IDEMPOTENCY_KEY', str(uuid4()))

        try:
            svc = _get_yookassa()
            svc.refund_payment(
                provider_payment_id=payment.provider_payment_id,
                amount=refund_amount,
                idempotency_key=idempotency_key,
            )
        except PaymentConfigError as exc:
            logger.error('YooKassa not configured: %s', exc)
            return error_response(
                'SERVICE_UNAVAILABLE',
                'Payment provider not configured.',
                status_code=503,
            )
        except PaymentClientError as exc:
            logger.error('YooKassa refund_payment failed: %s', exc)
            return error_response(
                'PAYMENT_PROVIDER_ERROR',
                'Payment provider error. Please try again.',
                status_code=502,
            )

        with transaction.atomic():
            payment.refunded_amount += refund_amount
            is_partial = payment.refunded_amount < payment.amount
            if payment.refunded_amount >= payment.amount:
                payment.status = Payment.Status.REFUNDED
            else:
                payment.status = Payment.Status.PARTIALLY_REFUNDED
            payment.save(update_fields=['status', 'refunded_amount', 'updated_at'])

            # Refund initiated by the client (or admin) — fire the same
            # PAYMENT_REFUNDED topic as the webhook path so notification
            # handlers don't care about the trigger source.
            emit_outbox_event(
                topic=OutboxEvent.Topic.PAYMENT_REFUNDED,
                data={
                    'payment_id': str(payment.id),
                    'appointment_id': str(payment.appointment_id),
                    'amount': str(refund_amount),
                    'is_partial': is_partial,
                },
                user_id=request.user.id,
                tenant_id=safe_tenant_id(
                    payment.appointment, context="payments.refund.client",
                ),
                actor="user",
            )

        logger.info(
            'Refund issued: payment_id=%s amount=%s', payment.id, refund_amount,
        )

        return success_response(PaymentDetailSerializer(payment).data)
