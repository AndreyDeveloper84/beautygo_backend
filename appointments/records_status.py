"""Derived status taxonomy for Records endpoints (#97 / W1 customer-records-flow).

Per Tau's customer-records-flow.md §R4 + memory ref
``project_records_voice_principles``. The Records UI badges expect a
single ``derived_status`` string drawn from the 14-value canonical
taxonomy below — combining ``Appointment.status``, payment lifecycle,
and cancellation attribution.

This module is **derive-only** — no DB writes, no model mutations.
The underlying ``Appointment.Status`` enum stays the 7-value
operational state machine. Tau's taxonomy is a *presentation*
projection of (appointment, payments, cancellation_reason,
cancelled_by) read together.

Canonical taxonomy
==================

Active states (no terminal flag set yet)

- ``pending``                      — awaiting specialist confirmation
- ``awaiting_payment``             — confirmed but unpaid
- ``confirmed``                    — booked + paid (or no payment required)
- ``rescheduled``                  — emitted when the booking has an
                                     ``old_start_at`` in event history
                                     (deferred — currently maps to
                                     ``confirmed``; surfaces in the
                                     response's ``original_datetime``
                                     field when known)
- ``in_progress``                  — appointment underway

Terminal-happy states

- ``completed``                    — service delivered, paid
- ``refund_completed``             — completed + full post-service refund
- ``partial_refund``               — any status + partially refunded payment

Terminal-cancelled states (attribution matters for UI copy)

- ``cancelled``                    — generic cancellation (no payment
                                     refund applied / didn't apply)
- ``customer_cancelled_with_refund`` — cancelled_by=client AND payment
                                       refunded
- ``no_fault_provider_cancelled``    — cancelled by salon/system with
                                       reason='specialist_departure'
                                       (Q2 cascade); full refund expected
- ``no_fault_reschedule_required``   — DEFERRED. Founder Q2 §3 future
                                       policy where master left mid-day
                                       and customer must reschedule
                                       through Ayla. Not emitted in MVP.
- ``no_show``                      — customer didn't show

Refund-in-flight + dispute states (NOT emitted in MVP — Payment model
doesn't carry these distinctions yet)

- ``refund_pending``               — refund initiated, not yet acked by
                                     YooKassa. Would require a
                                     ``Payment.Status.REFUNDING``
                                     transitional state we don't have.
- ``payment_disputed``             — customer flagged charge as
                                     unauthorized (placeholder).
- ``active_dispute``               — admin/CS escalation in progress
                                     (placeholder).
- ``chargeback_pending``           — bank-side chargeback in flight.
                                     Post-pilot only.

The deferred values are documented here as the canonical taxonomy so
W1's frontend can render badge strings without re-spec'ing the contract
when we add backend support.
"""
from __future__ import annotations

from typing import Iterable


# Public enum (string values) — the response field's ``derived_status``
# is one of these. Frontends key badge copy off these strings.
DERIVED_STATUSES: tuple[str, ...] = (
    # Active
    "pending",
    "awaiting_payment",
    "confirmed",
    "rescheduled",
    "in_progress",
    # Terminal-happy
    "completed",
    "refund_completed",
    "partial_refund",
    # Terminal-cancelled
    "cancelled",
    "customer_cancelled_with_refund",
    "no_fault_provider_cancelled",
    "no_fault_reschedule_required",
    "no_show",
    # Refund-in-flight / dispute (placeholders — see module docstring)
    "refund_pending",
    "payment_disputed",
    "active_dispute",
    "chargeback_pending",
)


def derive_status(appointment, payments: Iterable | None = None) -> str:
    """Return the canonical Records taxonomy string for ``appointment``.

    ``payments`` may be passed pre-fetched (caller has
    ``prefetch_related('payments')``); when None, falls back to a
    single query. The caller is expected to bulk-prefetch for list
    endpoints to avoid N+1.
    """
    from payments.models import Payment

    if payments is None:
        payments = list(appointment.payments.all())
    else:
        payments = list(payments)

    # Latest payment by created_at — typically the active row.
    latest = (
        max(payments, key=lambda p: p.created_at) if payments else None
    )

    status = appointment.status

    # Active / in-flight states — no terminal projection.
    if status == "pending":
        return "pending"
    if status == "awaiting_payment":
        return "awaiting_payment"
    if status == "confirmed":
        return "confirmed"
    if status == "in_progress":
        return "in_progress"

    if status == "completed":
        # Completed + post-service refund nuances.
        if latest is not None:
            if latest.status == Payment.Status.REFUNDED:
                return "refund_completed"
            if latest.status == Payment.Status.PARTIALLY_REFUNDED:
                return "partial_refund"
        return "completed"

    if status == "no_show":
        return "no_show"

    if status == "cancelled":
        # Attribution: cancelled_by_id vs client_id +
        # cancellation_reason determines which sub-status to surface.
        reason = (appointment.cancellation_reason or "").strip()
        if reason == "specialist_departure":
            return "no_fault_provider_cancelled"
        cancelled_by_id = getattr(appointment, "cancelled_by_id", None)
        client_id = appointment.client_id
        if (
            cancelled_by_id is not None
            and cancelled_by_id == client_id
            and latest is not None
            and latest.status in (
                Payment.Status.REFUNDED,
                Payment.Status.PARTIALLY_REFUNDED,
            )
        ):
            return "customer_cancelled_with_refund"
        return "cancelled"

    # Unknown / future operational status — return as-is. UI falls
    # back to the raw string.
    return status
