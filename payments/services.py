"""YooKassa payment service — provider abstraction layer."""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


def _get_commission_percent() -> Decimal:
    return Decimal(str(getattr(settings, 'BOOKING_COMMISSION_PERCENT', 8.0)))


class YooKassaService:
    """
    Thin wrapper around the yookassa SDK.

    All public methods raise YooKassaError on SDK / API failure.
    Call sites must catch this and return an appropriate HTTP response.
    """

    def __init__(self):
        import yookassa
        shop_id = getattr(settings, 'YOOKASSA_SHOP_ID', '')
        secret_key = getattr(settings, 'YOOKASSA_SECRET_KEY', '')
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

        # Split payment (transfer to specialist sub-account) if configured
        agent_id = getattr(settings, 'YOOKASSA_AGENT_ID', '')
        if agent_id:
            payload['transfers'] = [{
                'account_id': agent_id,
                'amount': {'value': str(specialist_income), 'currency': 'RUB'},
            }]

        payment = self._payment_cls.create(payload, idempotency_key)

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
        self._payment_cls.capture(
            provider_payment_id,
            {'amount': {'value': str(amount), 'currency': 'RUB'}},
            idempotency_key,
        )

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
        refund = self._refund_cls.create(
            {
                'payment_id': provider_payment_id,
                'amount': {'value': str(amount), 'currency': 'RUB'},
            },
            idempotency_key,
        )
        return {'refund_id': refund.id, 'status': refund.status}

    # ------------------------------------------------------------------
    # Status check
    # ------------------------------------------------------------------

    def get_payment_info(self, provider_payment_id: str) -> dict[str, Any]:
        """Fetch current payment state from YooKassa."""
        payment = self._payment_cls.find_one(provider_payment_id)
        return {
            'provider_payment_id': payment.id,
            'status': payment.status,
            'paid': getattr(payment, 'paid', False),
            'refunded_amount': Decimal(
                str(getattr(getattr(payment, 'refunded_amount', None), 'value', '0'))
            ),
        }
