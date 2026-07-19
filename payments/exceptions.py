"""Typed payment-domain exceptions.

Replaces bare ``Exception`` catches in payments views/services so the
caller can distinguish:

- **PaymentConfigError** — env not configured (missing SHOP_ID /
  SECRET_KEY). Distinct from runtime API failures so ops can alert on
  startup-time misconfiguration vs transient provider hiccups.
- **PaymentClientError** — wraps a provider-side failure (network,
  HTTP 5xx, malformed response). Maps to 502 ``PAYMENT_PROVIDER_ERROR``.
- **PaymentError** — base for any other domain-level payment problem
  (amount validation, status transitions). Maps to 422.

Pattern lifted from mysite/payments/exceptions.py (Formula Tela prod).
The taxonomy is the same minus the YClients-specific BookingError
branch — Ayla doesn't integrate with YClients.
"""
from __future__ import annotations


class PaymentError(Exception):
    """Base domain error for the payments subsystem."""


class PaymentConfigError(PaymentError):
    """YooKassa is not configured (empty SHOP_ID or SECRET_KEY).

    Surfaced when a request hits the provider before ops finished the
    env wiring. Production startup checks should refuse to boot if
    this would fire on the first /payments/create.
    """


class PaymentClientError(PaymentError):
    """Wraps a YooKassa SDK / API failure.

    Caught by views and rendered as 502 PAYMENT_PROVIDER_ERROR. The
    underlying ``yookassa.ApiError`` is chained via ``raise ... from``
    so Sentry shows both layers.
    """


class SpecialistPayoutNotConfiguredError(PaymentError):
    """The specialist has no YooKassa sub-account (split per-master, D8).

    Online payment for their bookings is UNAVAILABLE — the booking
    itself still works without prepayment (D6). Views map this to
    422 ONLINE_PAYMENT_UNAVAILABLE so the bot/client can fall back to
    the no-prepayment path instead of a generic provider error.
    """
