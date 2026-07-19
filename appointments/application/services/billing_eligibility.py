"""C1 Billing Eligibility — adapter for the W2 billing module.

Contract (PILOT_CONTRACTS §2, frozen): ``can_accept_booking(
specialist_id: UUID, tenant_id: UUID | None) -> EligibilityResult``
with ``EligibilityResult.ok / .reason == "SUBSCRIPTION_PAST_DUE"``.

The billing app is W2's deliverable and lands in ``billing/`` via dev.
This adapter isolates the integration in one place:

- **Fail-open in the pilot (C1):** the billing module missing, the
  function missing, or ANY technical error inside the call → allow the
  booking, log + Sentry alert (no personal/payment data in the alert).
- **Fail-closed on the business answer only:** ``ok=False`` raises
  ``BillingEligibilityError`` — the ONLY expected refusal reason today
  is ``SUBSCRIPTION_PAST_DUE``.

Only NEW booking creation is gated; existing bookings, reschedule,
cancel and complete never consult this adapter (C1 invariants).
"""
from __future__ import annotations

import logging
from uuid import UUID

from appointments.domain.exceptions import BillingEligibilityError

logger = logging.getLogger(__name__)

# Module path agreed with W2 per C1 ("Python-вызов внутри репо",
# producer billing/). If W2 lands a different location, only this
# import line changes.
_BILLING_MODULE = "billing.services"
_BILLING_FUNC = "can_accept_booking"


def _alert_fail_open(reason: str, **ctx) -> None:
    """C1 fail-open telemetry: technical problems go to Sentry WITHOUT
    personal or payment data (ids + coarse reason only)."""
    logger.warning('billing.eligibility_fail_open reason=%s %s', reason, ctx)
    try:
        import sentry_sdk
        sentry_sdk.capture_message(
            f'billing.eligibility_fail_open {reason} {ctx}', level='warning',
        )
    except Exception:  # noqa: BLE001 — sentry uninstalled/uninitialized
        pass


def check_billing_eligibility(
    specialist_id: UUID, tenant_id: UUID | None,
) -> None:
    """Raise BillingEligibilityError when billing refuses the booking.

    Silent no-op when billing says ok, is unavailable, or errors —
    per the C1 fail-open rule.
    """
    try:
        from importlib import import_module
        module = import_module(_BILLING_MODULE)
        can_accept_booking = getattr(module, _BILLING_FUNC)
    except (ImportError, AttributeError) as exc:
        _alert_fail_open(
            'billing_module_unavailable',
            specialist_id=str(specialist_id), detail=type(exc).__name__,
        )
        return

    try:
        result = can_accept_booking(
            specialist_id=specialist_id, tenant_id=tenant_id,
        )
    except Exception as exc:  # noqa: BLE001 — C1: expected billing
        # errors are not thrown outward; technical failure → fail-open.
        _alert_fail_open(
            'billing_call_error',
            specialist_id=str(specialist_id), detail=type(exc).__name__,
        )
        return

    if not getattr(result, 'ok', True):
        reason = getattr(result, 'reason', None) or 'SUBSCRIPTION_PAST_DUE'
        logger.info(
            'billing.eligibility_refused specialist=%s reason=%s',
            specialist_id, reason,
        )
        raise BillingEligibilityError(reason=reason)
