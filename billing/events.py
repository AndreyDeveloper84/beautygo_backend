"""C4 billing events — emitted through the transactional outbox.

PILOT_CONTRACTS v1.3.0 §5 + AMD-007 (full envelope) + AMD-008 (event_id
is a UUID4 string): ``emit_outbox_event`` builds the ADR-0009 envelope
(event_id/event_name/event_version/occurred_at/tenant_id/user_id/
actor/correlation_id/causation_id/data); the payloads below are the
``data`` contents from §5.

Topic registration (R-2 handoff): ``OutboxEvent.Topic`` +
``EVENT_VERSIONS`` are W1-owned files; their patch lands after this
branch merges. Until then the envelope builder raises ValueError on
these topic strings — callers MUST treat emission as best-effort (the
business fact is the DB row; alert via Sentry, don't roll back).

AMD-005: every ``specialist_id`` here is the Ayla **User UUID**.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from appointments.infrastructure.outbox.envelope import emit_outbox_event

if TYPE_CHECKING:  # pragma: no cover
    from billing.models import BookingFee, SpecialistSubscription

TOPIC_SUBSCRIPTION_ACTIVATED = "subscription.activated"
TOPIC_SUBSCRIPTION_PAST_DUE = "subscription.past_due"
TOPIC_FEE_CHARGED = "billing.fee_charged"


def emit_fee_charged(fee: "BookingFee", *, correlation_id=None) -> None:
    """billing.fee_charged — {specialist_id, appointment_id, amount, period}.

    Stable event_id = OutboxEvent PK (UUID4, AMD-008). A retry cannot
    spawn a second event: the C4 invariant UNIQUE(appointment_id) makes
    re-accrual a no-op, so emission happens exactly once per fee row.
    """
    emit_outbox_event(
        topic=TOPIC_FEE_CHARGED,
        data={
            # The master who performed the visit (user UUID), NOT the
            # salon payer — salon fees notify the acting specialist.
            "specialist_id": str(fee.appointment.specialist.user_id),
            "appointment_id": str(fee.appointment_id),
            "amount": f"{fee.amount:.2f}",
            "period": fee.period_start.isoformat(),
        },
        user_id=fee.appointment.specialist.user_id,
        tenant_id=fee.appointment.tenant_id,
        actor="system",
        correlation_id=correlation_id,
    )


def emit_subscription_activated(
    subscription: "SpecialistSubscription", *, correlation_id=None,
) -> None:
    """subscription.activated — {specialist_id, tariff, period_end}."""
    emit_outbox_event(
        topic=TOPIC_SUBSCRIPTION_ACTIVATED,
        data={
            "specialist_id": str(subscription.user_id),
            "tariff": subscription.tariff.code,
            "period_end": (
                subscription.current_period_end.isoformat()
                if subscription.current_period_end else None
            ),
        },
        user_id=subscription.user_id,
        tenant_id=subscription.tenant_id,
        actor="system",
        correlation_id=correlation_id,
    )


def emit_subscription_past_due(
    subscription: "SpecialistSubscription", *, debt_amount, failed_attempts: int,
    correlation_id=None,
) -> None:
    """subscription.past_due — {specialist_id, debt_amount, failed_attempts}."""
    emit_outbox_event(
        topic=TOPIC_SUBSCRIPTION_PAST_DUE,
        data={
            "specialist_id": str(subscription.user_id),
            "debt_amount": f"{debt_amount:.2f}",
            "failed_attempts": failed_attempts,
        },
        user_id=subscription.user_id,
        tenant_id=subscription.tenant_id,
        actor="system",
        correlation_id=correlation_id,
    )
