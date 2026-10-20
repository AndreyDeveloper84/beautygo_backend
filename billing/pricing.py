"""Period price of a subscription (DRF-2984, owner decisions 20.10).

price = plan price (or the subscription's override)
      + extra master price (or override) × max(0, masters − plan limit)

Masters are counted by the catalog itself, never sent by the bot (В-3):
the tenant's masters that are on sale right now — published, in the pool,
accepting bookings (``users.sellable.sellable_q``). A paused master is
not counted while paused; receptionists and admins have no specialist
profile and are never counted. Snapshot at invoice time, no proration
inside a period.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from billing.models import SpecialistSubscription, quantize_money

_ZERO = Decimal("0.00")


@dataclass(frozen=True, slots=True)
class PeriodPrice:
    masters_count: int
    base_amount: Decimal
    extra_masters: int
    extra_masters_amount: Decimal
    total: Decimal


def billable_masters_count(subscription: SpecialistSubscription) -> int:
    """Masters the period is priced on. A personal account is one master."""
    if subscription.tenant_id is None:
        return 1
    from users.models import SpecialistProfile
    from users.sellable import sellable_q

    return (
        SpecialistProfile.objects
        .filter(sellable_q(), tenant_id=subscription.tenant_id)
        .count()
    )


def period_price(
    subscription: SpecialistSubscription, *, masters_count: int | None = None,
) -> PeriodPrice:
    tariff = subscription.tariff
    if masters_count is None:
        masters_count = billable_masters_count(subscription)
    base = (
        subscription.price_override
        if subscription.price_override is not None else tariff.price
    )
    unit = (
        subscription.extra_master_price_override
        if subscription.extra_master_price_override is not None
        else tariff.extra_master_price
    )
    extra = max(0, masters_count - tariff.max_masters)
    # No extra price on the plan (solo) → masters above the limit are not
    # billed here; the conversion to salon (В-2) is the bot's gate.
    extra_amount = quantize_money(unit * extra) if (unit is not None and extra) else _ZERO
    base = quantize_money(base)
    return PeriodPrice(
        masters_count=masters_count,
        base_amount=base,
        extra_masters=extra if unit is not None else 0,
        extra_masters_amount=extra_amount,
        total=quantize_money(base + extra_amount),
    )
