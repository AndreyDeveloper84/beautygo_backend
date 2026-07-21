"""YooKassa client for billing — specialist-side money (AYLA-DEC-0007).

Covers the subscription flow: card binding via ``save_payment_method``
and monthly recurrent auto-charges. Deliberately separate from
``payments/services.py`` (W1 owns the client→specialist flow); stream
isolation beats reuse here — W1 refactors must not break billing.
"""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class BillingPaymentConfigError(Exception):
    """YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY are empty (wiring bug)."""


class BillingPaymentClientError(Exception):
    """SDK / transport failure talking to YooKassa (transient)."""


class BillingYooKassaClient:
    """Thin wrapper over the yookassa SDK for billing payments."""

    def __init__(self) -> None:
        import yookassa

        shop_id = getattr(settings, "YOOKASSA_SHOP_ID", "")
        secret_key = getattr(settings, "YOOKASSA_SECRET_KEY", "")
        if not shop_id or not secret_key:
            raise BillingPaymentConfigError(
                "YooKassa credentials not configured "
                "(YOOKASSA_SHOP_ID / YOOKASSA_SECRET_KEY are empty)"
            )
        yookassa.Configuration.configure(shop_id, secret_key)
        self._payment_cls = yookassa.Payment

    @staticmethod
    def _wrap(call) -> Any:
        try:
            return call()
        except Exception as exc:  # noqa: BLE001 — wrap SDK + transport
            raise BillingPaymentClientError(f"YooKassa call failed: {exc}") from exc

    def create_setup_payment(
        self, *, amount, description: str, return_url: str,
        idempotency_key: str, receipt: dict | None, metadata: dict,
    ) -> dict:
        """First master payment: capture + save_payment_method (D7).

        The master is redirected to ``confirmation_url``; on success the
        webhook (payment.succeeded) persists the payment_method_id.
        """
        payload: dict[str, Any] = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "save_payment_method": True,
            "description": description,
            "metadata": metadata,
        }
        if receipt:
            payload["receipt"] = receipt
        payment = self._wrap(
            lambda: self._payment_cls.create(payload, idempotency_key)
        )
        confirmation = getattr(payment, "confirmation", None)
        return {
            "provider_payment_id": payment.id,
            "confirmation_url": getattr(confirmation, "confirmation_url", "") or "",
            "status": payment.status,
        }

    def create_recurrent_payment(
        self, *, amount, payment_method_id: str, description: str,
        idempotency_key: str, receipt: dict | None, metadata: dict,
    ) -> dict:
        """Monthly auto-charge against the saved payment_method_id."""
        payload: dict[str, Any] = {
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "payment_method_id": payment_method_id,
            "capture": True,
            "description": description,
            "metadata": metadata,
        }
        if receipt:
            payload["receipt"] = receipt
        payment = self._wrap(
            lambda: self._payment_cls.create(payload, idempotency_key)
        )
        return {
            "provider_payment_id": payment.id,
            "status": payment.status,
        }

    def get_payment_info(self, provider_payment_id: str) -> dict:
        """Re-fetch provider state — webhooks verify, never trust payload."""
        payment = self._wrap(lambda: self._payment_cls.find_one(provider_payment_id))
        method = getattr(payment, "payment_method", None)
        card = getattr(method, "card", None) if method else None
        card_last4 = ""
        card_brand = ""
        if card is not None:
            card_last4 = getattr(card, "last4", "") or ""
            # YooKassa exposes the scheme as brand (or card_type by SDK ver).
            card_brand = (
                getattr(card, "brand", "") or getattr(card, "card_type", "") or ""
            )
        return {
            "provider_payment_id": payment.id,
            "status": payment.status,
            "paid": getattr(payment, "paid", False),
            "payment_method_id": getattr(method, "id", "") if method else "",
            "payment_method_saved": bool(getattr(method, "saved", False)) if method else False,
            "card_last4": card_last4,
            "card_brand": card_brand,
        }
