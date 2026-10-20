"""Billing switches (DRF-2984, PR-C1) — read at call time, never cached.

``BILLING_ENABLED`` turns the subscription accounting on;
``BILLING_CHARGES_ENABLED`` lets billing/ talk to YooKassa. Both are off
by default, which is the pilot's free mode. See settings/base.py.
"""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class BillingChargesDisabled(Exception):
    """A door of billing/ was asked to reach YooKassa with charges off."""


def billing_enabled() -> bool:
    return bool(getattr(settings, "BILLING_ENABLED", False))


def charges_enabled() -> bool:
    return bool(getattr(settings, "BILLING_CHARGES_ENABLED", False))


def require_charges_enabled() -> None:
    """Stop before any YooKassa work (and before any row it would need)."""
    if not charges_enabled():
        raise BillingChargesDisabled(
            "BILLING_CHARGES_ENABLED is off — no YooKassa calls from billing/",
        )


def check_flag_pair() -> None:
    """Startup guard: charges without accounting is a config error."""
    if charges_enabled() and not billing_enabled():
        raise ImproperlyConfigured(
            "BILLING_CHARGES_ENABLED=true requires BILLING_ENABLED=true "
            "(money must not move with billing accounting switched off)",
        )
