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
from django.utils import timezone

from .exceptions import (
    PaymentClientError,
    PaymentConfigError,
    SpecialistPayoutNotConfiguredError,
)

logger = logging.getLogger(__name__)


def get_platform_fee(amount: Decimal) -> Decimal:
    """Flat platform fee per successful booking (AYLA-DEC-0001/D1: 90₽).

    Replaces the pre-pilot 8% commission. Capped at the charge amount so
    ``specialist_income`` never goes negative (data contract §1:
    negative amounts are forbidden).
    """
    flat = Decimal(str(getattr(settings, 'BOOKING_PLATFORM_FEE_RUB', '90.00')))
    return min(flat, amount).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


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
        specialist_account_id: str = "",
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
            specialist_account_id: YooKassa sub-account of THIS specialist
                (SpecialistProfile.yookassa_account_id) — split per-master
                (AYLA-DEC-0008/D8): ``specialist_income`` is transferred to
                the master's sub-account at capture, the platform keeps the
                flat fee. Empty → SpecialistPayoutNotConfiguredError:
                online payment is unavailable for this specialist, the
                no-prepayment booking path (D6) still works.

        Returns:
            dict with keys: provider_payment_id, confirmation_url, status
        """
        if not specialist_account_id:
            raise SpecialistPayoutNotConfiguredError(
                "Specialist has no YooKassa sub-account — "
                "online payment unavailable (booking without prepayment works)"
            )
        platform_fee = get_platform_fee(amount)
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

        # Split payment per-master (D8): transfer the specialist's income
        # to their own sub-account; the platform keeps the flat fee.
        payload['transfers'] = [{
            'account_id': specialist_account_id,
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
    ) -> dict[str, Any]:
        """Capture a previously held payment.

        Returns the provider payment state (``status``) so the caller
        (capture task) can advance local state without a second fetch.
        """
        try:
            payment = self._payment_cls.capture(
                provider_payment_id,
                {'amount': {'value': str(amount), 'currency': 'RUB'}},
                idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise PaymentClientError(
                f"YooKassa capture_payment failed: {exc}"
            ) from exc
        return {
            'provider_payment_id': getattr(payment, 'id', provider_payment_id),
            'status': getattr(payment, 'status', ''),
        }

    # ------------------------------------------------------------------
    # Cancel (release a hold, two-stage payment)
    # ------------------------------------------------------------------

    def cancel_payment(
        self,
        provider_payment_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Cancel a payment in ``waiting_for_capture`` — releases the hold.

        Only possible BEFORE capture; after ``succeeded`` use refund.
        Returns the provider payment state for local convergence.
        """
        try:
            payment = self._payment_cls.cancel(
                provider_payment_id,
                idempotency_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise PaymentClientError(
                f"YooKassa cancel_payment failed: {exc}"
            ) from exc
        return {
            'provider_payment_id': getattr(payment, 'id', provider_payment_id),
            'status': getattr(payment, 'status', ''),
        }

    # ------------------------------------------------------------------
    # Card binding (C7.2) — zero-amount, save_payment_method: true
    # ------------------------------------------------------------------

    def create_card_binding(
        self,
        *,
        user_id: uuid.UUID,
        consent_version: str,
        return_url: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Create a zero-amount binding payment (YooKassa card setup).

        A binding is a SEPARATE voluntary action (consent boundary,
        C7.2) — never a side effect of a service payment. The method is
        persisted locally ONLY when the webhook later confirms
        ``payment_method.saved == true``. Consent proof travels in the
        provider metadata so the webhook can store it verbatim.
        """
        payload: dict[str, Any] = {
            'amount': {'value': '0.00', 'currency': 'RUB'},
            'confirmation': {
                'type': 'redirect',
                'return_url': return_url,
            },
            'capture': True,
            'save_payment_method': True,
            'description': 'Ayla: привязка карты',
            'metadata': {
                'purpose': 'card_binding',
                'ayla_user_id': str(user_id),
                'consent_version': consent_version,
                # Consent timestamp == the setup moment (the user just
                # accepted the consent text); the webhook stores it
                # verbatim on the saved method.
                'consented_at': timezone.now().isoformat(),
            },
        }
        try:
            payment = self._payment_cls.create(payload, idempotency_key)
        except Exception as exc:  # noqa: BLE001
            raise PaymentClientError(
                f"YooKassa create_card_binding failed: {exc}"
            ) from exc

        confirmation_url = ''
        if hasattr(payment, 'confirmation') and payment.confirmation:
            confirmation_url = getattr(payment.confirmation, 'confirmation_url', '')
        return {
            'provider_payment_id': payment.id,
            'confirmation_url': confirmation_url,
            'status': payment.status,
        }

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
        expires_at = getattr(payment, 'expires_at', None)
        # C7.2 — card-binding confirmations carry the saved method's
        # details; booking payments leave these empty.
        method = getattr(payment, 'payment_method', None)
        card = getattr(method, 'card', None) if method else None
        metadata = getattr(payment, 'metadata', None) or {}
        return {
            'provider_payment_id': payment.id,
            'status': payment.status,
            'paid': getattr(payment, 'paid', False),
            # Capture deadline for two-stage payments (D9): the hold
            # auto-cancels after this moment — capture must be planned
            # relative to it (minus the safety buffer), never to a
            # hardcoded "7 days". None for single-stage payments.
            'expires_at': expires_at,
            'refunded_amount': Decimal(
                str(getattr(getattr(payment, 'refunded_amount', None), 'value', '0'))
            ),
            'metadata': metadata,
            'payment_method': {
                'id': getattr(method, 'id', '') if method else '',
                'saved': bool(getattr(method, 'saved', False)) if method else False,
                'last4': getattr(card, 'last4', '') if card else '',
                'brand': getattr(card, 'brand', '') if card else '',
            },
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
        # Stash the (optionally injected) instance; do NOT construct a
        # default ``YooKassaService()`` here. The constructor raises
        # ``PaymentConfigError`` when YOOKASSA_SHOP_ID / SECRET_KEY are
        # empty, and constructing eagerly here would prevent the
        # validation steps in ``execute`` (404 / 409 paths) from running
        # in deployments where the provider isn't configured — the
        # error path correct for visibility/state guards must not be
        # gated on provider config.
        self._yookassa = yookassa

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
            "Ayla: "
            f"{appointment.service.name if appointment.service_id else appointment.snapshot_service_name}"
            f" у {appointment.specialist.display_name}"
        )
        # 54-ФЗ receipt — mandatory for every YooKassa payment session,
        # not just first-attempt.
        receipt = build_appointment_receipt(appointment, appointment.price)

        # Lazy provider instantiation: only after the validation steps
        # above have passed. ``PaymentConfigError`` (missing credentials)
        # surfaces as 503 to the caller; getting here means the request
        # is well-formed AND the resource is reachable, so a 503 is the
        # accurate signal.
        yookassa = self._yookassa or YooKassaService()
        result = yookassa.create_payment(
            amount=appointment.price,
            appointment_id=appointment.id,
            description=description,
            return_url=return_url,
            idempotency_key=idempotency_key,
            capture=False,
            receipt=receipt,
            specialist_account_id=getattr(
                appointment.specialist, 'yookassa_account_id', '',
            ),
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


# ---------------------------------------------------------------------------
# Capture scheduling (D9) + hold cancellation — appointment lifecycle hooks
# ---------------------------------------------------------------------------


def compute_capture_at(*, completed_at, expires_at) -> "Any":
    """When the capture task should fire for a completed appointment.

    Per the capture-strategy ADR (D9): never plan against a hardcoded
    "7 days" — the real hold deadline is YooKassa's ``expires_at`` and
    varies by payment method (2h … 7d). The delay is clamped to
    ``expires_at − CAPTURE_SAFETY_BUFFER_MINUTES`` so a long configured
    delay cannot outlive a short hold. With the pilot default
    ``CAPTURE_DELAY_HOURS=0`` this resolves to "now" in practice.
    """
    from datetime import timedelta as _td

    delay = float(getattr(settings, 'CAPTURE_DELAY_HOURS', 0))
    buffer_min = int(getattr(settings, 'CAPTURE_SAFETY_BUFFER_MINUTES', 60))
    capture_at = completed_at + _td(hours=delay)
    if expires_at is not None:
        capture_at = min(capture_at, expires_at - _td(minutes=buffer_min))
    return capture_at


def schedule_capture_for_appointment(appointment, *, completed_at) -> int:
    """Enqueue the deferred capture task for every held payment of a
    just-completed appointment. Returns the number of tasks enqueued.

    Called from the complete() write path AFTER the booking transitioned
    (the appointment row is already ``completed``). Only payments that
    are actually held (AUTHORIZED + capture_state=scheduled) qualify —
    a pending/unpaid or already-captured payment is left alone, which
    also makes a repeated complete() call a no-op here.
    """
    from payments.models import Payment
    from payments.tasks import capture_payment_task

    now = completed_at
    enqueued = 0
    held = appointment.payments.filter(
        status=Payment.Status.AUTHORIZED,
        capture_state=Payment.CaptureState.SCHEDULED,
    )
    for payment in held:
        capture_at = compute_capture_at(
            completed_at=now, expires_at=payment.yookassa_expires_at,
        )
        countdown = max(0.0, (capture_at - now).total_seconds())
        payment.capture_scheduled_for = capture_at
        payment.save(update_fields=['capture_scheduled_for', 'updated_at'])
        capture_payment_task.apply_async(
            args=[str(payment.id)], countdown=countdown,
        )
        enqueued += 1
        logger.info(
            'capture.scheduled payment_id=%s appointment_id=%s '
            'capture_at=%s countdown=%.0fs',
            payment.id, appointment.id, capture_at.isoformat(), countdown,
        )
    return enqueued


def cancel_authorized_hold_for_appointment(
    appointment, *, yookassa: YooKassaService | None = None,
) -> int:
    """Release the hold of every authorized payment of a cancelled
    appointment (acceptance #5: booking cancel ⇒ hold auto-cancelled).

    Best-effort by design: a provider outage must NOT block the booking
    cancellation itself — the booking is already cancelled when this
    runs. Failures are logged and left in capture_state=scheduled for
    the reconciliation job to finish (the hold also auto-expires at
    ``expires_at``). Returns the number of holds released.

    Idempotency: the key is stable per payment (``cancel-{payment.id}``),
    so a retried call converges provider-side; already-terminal payments
    (canceled / captured) are skipped locally.
    """
    from payments.models import Payment

    released = 0
    held = appointment.payments.filter(
        status=Payment.Status.AUTHORIZED,
        capture_state=Payment.CaptureState.SCHEDULED,
    )
    for payment in held:
        try:
            svc = yookassa or YooKassaService()
            svc.cancel_payment(
                provider_payment_id=payment.provider_payment_id,
                idempotency_key=f'cancel-{payment.id}',
            )
        except (PaymentClientError, PaymentConfigError) as exc:
            logger.error(
                'hold.cancel_failed payment_id=%s appointment_id=%s: %s '
                '— left for reconciliation',
                payment.id, appointment.id, exc,
            )
            continue
        payment.capture_state = Payment.CaptureState.CANCELED
        payment.save(update_fields=['capture_state', 'updated_at'])
        released += 1
        logger.info(
            'hold.canceled payment_id=%s appointment_id=%s',
            payment.id, appointment.id,
        )
    return released
