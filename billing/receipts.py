"""54-ФЗ fiscal receipt for the platform → specialist service (D7).

The payer here is the SPECIALIST (subscription owner) — unlike
``payments/services.build_appointment_receipt`` where the payer is the
client. Same YooKassa receipt shape (customer + items), forwarded to
the OFD. Receipt per charge: subscription and fees are one platform
service line at the invoice total (pilot simplification — the invoice
carries the breakdown).
"""
from __future__ import annotations

from typing import Any

from django.conf import settings


def build_platform_receipt(subscription, *, amount, description: str) -> dict[str, Any]:
    """YooKassa ``receipt`` payload with the master as the customer."""
    user = subscription.user
    customer: dict[str, str] = {}
    phone = getattr(user, "phone", "") or ""
    if phone:
        customer["phone"] = phone
    email = getattr(user, "email", "") or ""
    if email:
        customer["email"] = email
    if not customer:
        # YooKassa requires at least one contact; placeholder keeps the
        # API call alive and surfaces the data-quality bug in logs.
        customer["phone"] = "+70000000000"

    vat_code = int(getattr(settings, "YOOKASSA_VAT_CODE", 1))
    return {
        "customer": customer,
        "items": [{
            "description": description[:128],
            "quantity": "1.00",
            "amount": {"value": f"{amount:.2f}", "currency": "RUB"},
            "vat_code": vat_code,
            "payment_mode": "full_payment",
            "payment_subject": "service",
        }],
    }
