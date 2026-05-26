"""YooKassa payment service — provider abstraction layer."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import transaction

from .exceptions import PaymentClientError, PaymentConfigError

logger = logging.getLogger(__name__)


def _get_commission_percent() -> Decimal:
    return Decimal(str(getattr(settings, 'BOOKING_COMMISSION_PERCENT', 8.0)))


class YooKassaService:
    """
    Thin wrapper around the yookassa SDK.

    Public methods raise:
    - PaymentConfigError when YOOKASSA_SHOP_ID / SECRET_KEY are empty
      (startup-time wiring bug)
    - PaymentClientError on any SDK / HTTP failure (transient provider
      issue)
    Call sites map these to 503 / 502 respectively.
    """

    def __init__(self):
        import yookassa
        shop_id = getattr(settings, 'YOOKASSA_SHOP_ID', '')
        secret_key = getattr(settings, 'YOOKASSA_SECRET_KEY', '')
        if not shop_id or not secret_key:
            raise PaymentConfigError(
                "YooKassa credentials not configured "
                "(YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY are empty)"
            )
        yookassa.Configuration.configure(shop_id, secret_key)
        self._payment_cls = yookassa.Payment
        self._refund_cls = yookassa.Refund

    # ------------------------------------------------------------------
    # Payment creation
    # ------------------------------------------------------------------

    def create_payment(
        self,
        amount: Decimal,
        appointment_id: uuid.UUID,
        description: str,
        return_url: str,
        idempotency_key: str,
        capture: bool = False,
        receipt: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        Create a YooKassa payment.

        Args:
            amount: Total charge amount in RUB.
            appointment_id: Used as metadata reference.
            description: Human-readable payment description.
            return_url: Redirect after payment.
            idempotency_key: Prevents duplicate charges.
            capture: False = hold (two-stage), True = instant capture.
            receipt: 54-ФЗ fiscal receipt payload (customer + items). When
                provided, YooKassa forwards it to the OFD (operator
                фискальных данных). Mandatory for production payments
                in Russia — without it the merchant is non-compliant.
                Build via ``build_appointment_receipt(appointment, ...)``.

        Returns:
            dict with keys: provider_payment_id, confirmation_url, status
        """
        commission_pct = _get_commission_percent()
        platform_fee = (amount * commission_pct / 100).quantize(
            Decimal('0.01'), rounding=ROUND_HALF_UP,
        )
        specialist_income = amount - platform_fee

        payload: dict[str, Any] = {
            'amount': {'value': str(amount), 'currency': 'RUB'},
            'confirmation': {
                'type': 'redirect',
                'return_url': return_url,
            },
            'capture': capture,
            'description': description,
            'metadata': {
                'appointment_id': str(appointment_id),
                'platform_fee': str(platform_fee),
                'specialist_income': str(specialist_income),
            },
        }

        # 54-ФЗ fiscal receipt — required for prod RF deployments.
        # Empty/None = developer skipped on purpose (e.g. test settings
        # with YOOKASSA_FISCAL_RECEIPT_REQUIRED=False); production checks
        # via call site in views.py before calling us.
        if receipt:
            payload['receipt'] = receipt

        # Split payment (transfer to specialist sub-account) if configured
        agent_id = getattr(settings, 'YOOKASSA_AGENT_ID', '')
        if agent_id:
            payload['transfers'] = [{
                'account_id': agent_id,
                'amount': {'value': str(specialist_income), 'currency': 'RUB'},
            }]

        try:
            payment = self._payment_cls.create(payload, idempotency_key)
        except Exception as exc:  # noqa: BLE001 — wrap SDK + transport
            raise PaymentClientError(
                f"YooKassa create_payment failed: {exc}"
            ) from exc

        confirmation_url = ''
        if hasattr(payment, 'confirmation') and payment.confirmation:
            confirmation_url = getattr(payment.confirmation, 'confirmation_url', '')

        return {
            'provider_payment_id': payment.id,
            'confirmation_url': confirmation_url,
            'status': payment.status,
            'platform_fee': platform_fee,
            'specialist_income': specialist_income,
        }

    # ------------------------------------------------------------------
    # Capture (two-stage payment)
    # ------------------------------------------------------------------

    def capture_payment(
        self,
        provider_payment_id: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> None:
        """Capture a previously held payment."""
        try:
            self._payment_cls.capture(
                provider_payment_id,
                {'amount': {'value': str(amount), 'currency': 'RUB'}},
                idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise PaymentClientError(
                f"YooKassa capture_payment failed: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Refund
    # ------------------------------------------------------------------

    def refund_payment(
        self,
        provider_payment_id: str,
        amount: Decimal,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create a refund for a payment."""
        try:
            refund = self._refund_cls.create(
                {
                    'payment_id': provider_payment_id,
                    'amount': {'value': str(amount), 'currency': 'RUB'},
                },
                idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise PaymentClientError(
                f"YooKassa refund_payment failed: {exc}"
            ) from exc
        return {'refund_id': refund.id, 'status': refund.status}

    # ------------------------------------------------------------------
    # Status check
    # ------------------------------------------------------------------

    def get_payment_info(self, provider_payment_id: str) -> dict[str, Any]:
        """Fetch current payment state from YooKassa."""
        try:
            payment = self._payment_cls.find_one(provider_payment_id)
        except Exception as exc:  # noqa: BLE001
            raise PaymentClientError(
                f"YooKassa find_one failed: {exc}"
            ) from exc
        return {
            'provider_payment_id': payment.id,
            'status': payment.status,
            'paid': getattr(payment, 'paid', False),
            'refunded_amount': Decimal(
                str(getattr(getattr(payment, 'refunded_amount', None), 'value', '0'))
            ),
        }


# ---------------------------------------------------------------------------
# 54-ФЗ fiscal receipt builder
# ---------------------------------------------------------------------------


def build_appointment_receipt(appointment, amount: Decimal) -> dict[str, Any]:
    """Build a YooKassa ``receipt`` payload for a service Appointment.

    Required by Russian fiscal law 54-ФЗ for any online card payment.
    YooKassa relays this to the OFD (фискальный оператор) which prints
    the receipt in tax authority records. Without it, the merchant is
    non-compliant.

    Receipt shape (per YooKassa docs § Чек):
        {
            "customer": { "phone": <E.164>, "email"?: ... },
            "items": [{
                "description": <≤128 chars>,
                "quantity": "1.00",
                "amount": {"value": <decimal as string>, "currency": "RUB"},
                "vat_code": <int>,
                "payment_mode": "full_payment",
                "payment_subject": "service",
            }]
        }

    VAT code defaults to 1 ("без НДС" — for самозанятые / УСН). Override
    via ``YOOKASSA_VAT_CODE`` env when the merchant moves to OSNO.
    """
    user = appointment.client
    customer: dict[str, str] = {}
    phone = getattr(user, "phone", "") or ""
    if phone:
        customer["phone"] = phone
    email = getattr(user, "email", "") or ""
    if email:
        customer["email"] = email

    # YooKassa requires at least one of phone/email. If both are empty
    # (shouldn't happen — phone is enforced at registration) fall back
    # to a placeholder so the API call doesn't fail; the underlying
    # data quality bug surfaces in the exception/logs.
    if not customer:
        customer["phone"] = "+70000000000"

    service_name = (
        appointment.service.name if appointment.service_id
        else getattr(appointment, "snapshot_service_name", "") or "Услуга"
    )

    vat_code = int(getattr(settings, "YOOKASSA_VAT_CODE", 1))

    return {
        "customer": customer,
        "items": [{
            "description": service_name[:128],
            "quantity": "1.00",
            "amount": {
                "value": f"{amount:.2f}", "currency": "RUB",
            },
            "vat_code": vat_code,
            "payment_mode": "full_payment",
            "payment_subject": "service",
        }],
    }


# ---------------------------------------------------------------------------
# Payment retry — shared between client-mobile and internal/bot views
# ---------------------------------------------------------------------------


class PaymentRetryStatusError(Exception):
    """Retry refused because the source payment or its appointment is
    not in a retry-eligible state.

    Carries ``http_status`` so the calling view can translate to the
    correct REST status code without re-discriminating the cause.
    """

    def __init__(self, message: str, *, http_status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class PaymentRetryResult:
    payment_id: UUID
    confirmation_url: str
    amount: float


class PaymentRetryService:
    """Issue a fresh YooKassa session for a previously-failed Payment.

    Extracted from ``PaymentRetryView.post`` so the mobile path and the
    internal bot path (``InternalPaymentRetryView``) share one
    transactional + validation contract. The view is now thin: parse →
    auth → ``service.execute(...)`` → translate exception or return
    ``PaymentRetryResult`` to the response envelope.

    The service does NOT swallow exceptions — callers translate
    ``Payment.DoesNotExist`` to 404, ``PaymentRetryStatusError`` to its
    carried ``http_status`` (409), ``PaymentConfigError`` to 503, and
    ``PaymentClientError`` to 502. This mirrors the original
    behaviour byte-for-byte.
    """

    def __init__(self, yookassa: YooKassaService | None = None) -> None:
        self._yookassa = yookassa or YooKassaService()

    def execute(
        self,
        *,
        user,
        payment_id: UUID,
        return_url: str,
        idempotency_key: str,
    ) -> PaymentRetryResult:
        # Lazy import — payments.services is imported by views/tests; we
        # don't want to introduce a hard-edge dep on appointments here
        # at module load (and ``Payment`` already lives in this app).
        from appointments.models import Appointment
        from payments.models import Payment

        old_payment = (
            Payment.objects
            .select_related(
                'appointment',
                'appointment__specialist',
                'appointment__service',
            )
            .get(pk=payment_id, appointment__client=user)
        )

        if old_payment.status != Payment.Status.FAILED:
            raise PaymentRetryStatusError(
                f"Cannot retry payment in status '{old_payment.status}'. "
                "Only failed payments may be retried.",
            )

        appointment = old_payment.appointment
        if appointment.status not in (
            Appointment.Status.PENDING,
            Appointment.Status.AWAITING_PAYMENT,
        ):
            raise PaymentRetryStatusError(
                f"Appointment in status '{appointment.status}' cannot "
                "accept a new payment.",
            )

        description = (
            f"Ayla: {appointment.service.name} у "
            f"{appointment.specialist.display_name}"
        )
        # 54-ФЗ receipt — mandatory for every YooKassa payment session,
        # not just first-attempt.
        receipt = build_appointment_receipt(appointment, appointment.price)

        result = self._yookassa.create_payment(
            amount=appointment.price,
            appointment_id=appointment.id,
            description=description,
            return_url=return_url,
            idempotency_key=idempotency_key,
            capture=False,
            receipt=receipt,
        )

        with transaction.atomic():
            new_payment = Payment.objects.create(
                appointment=appointment,
                amount=appointment.price,
                status=Payment.Status.PENDING,
                specialist_income=result['specialist_income'],
                platform_fee=result['platform_fee'],
                provider='yookassa',
                provider_payment_id=result['provider_payment_id'],
                provider_client_secret=result['confirmation_url'],
            )
            # Re-enter awaiting_payment if appointment fell back to
            # pending after the original failure (defensive — usually
            # already awaiting_payment).
            if appointment.status == Appointment.Status.PENDING:
                appointment.status = Appointment.Status.AWAITING_PAYMENT
                appointment.save(update_fields=['status'])

        logger.info(
            'Payment retry: old_payment_id=%s new_payment_id=%s '
            'appointment_id=%s amount=%s',
            old_payment.id, new_payment.id, appointment.id,
            new_payment.amount,
        )

        return PaymentRetryResult(
            payment_id=new_payment.id,
            confirmation_url=result['confirmation_url'],
            amount=float(new_payment.amount),
        )
