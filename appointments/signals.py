"""Slot-cache invalidation for schedule changes (DRF-1062).

``users.schedule_api`` invalidates the slot cache inline, in the view,
because it is the only writer of the weekly template. The two models
added by DRF-1062 have several writers — the salon-admin API, the
provisioning command, Django admin — so invalidation lives on the model
signal instead. Missing it would leave an administrator clicking "закрыть
салон" and watching the bot keep offering the closed day for up to
``SLOTS_CACHE_TTL_SECONDS``, with no feedback that anything happened.

Invalidation is best-effort by design (``SlotCacheService`` swallows and
logs cache errors): a cache outage must degrade slot freshness, never
fail a schedule edit.
"""
from __future__ import annotations

import logging

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from appointments.models import SpecialistScheduleException, TenantClosure

logger = logging.getLogger(__name__)


def _invalidate(specialist_id, target_date) -> None:
    from appointments.infrastructure.cache.slot_cache import SlotCacheService

    SlotCacheService().invalidate(specialist_id, target_date)


@receiver(post_save, sender=SpecialistScheduleException)
@receiver(post_delete, sender=SpecialistScheduleException)
def invalidate_on_schedule_exception(sender, instance, **kwargs) -> None:
    """One specialist, one date — the narrowest possible invalidation."""
    _invalidate(instance.specialist_id, instance.date)


@receiver(post_save, sender=TenantClosure)
@receiver(post_delete, sender=TenantClosure)
def invalidate_on_tenant_closure(sender, instance, **kwargs) -> None:
    """Every specialist of the tenant, for the closed date.

    This is a fan-out over cache KEYS, not over stored rows — the closure
    itself stays a single row. Worst case if it under-reaches is bounded
    by the 60s slot TTL; a fan-out of data would have no such bound.
    """
    from users.models import SpecialistProfile

    specialist_ids = (
        SpecialistProfile.objects
        .filter(tenant_id=instance.tenant_id)
        .values_list('id', flat=True)
    )
    for specialist_id in specialist_ids:
        _invalidate(specialist_id, instance.date)
