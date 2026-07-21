"""Shared service_id resolver for the INTERNAL booking surface (AMD-019).

The pilot catalog lives in ``SalonService`` + ``SpecialistService`` (the
canonical S3A layers, #1044) while the booking contour historically
resolved ``service_id`` against the marketplace ``Service`` model — zero
rows in the pilot. Per the owner GO (2026-07-21, contracts v1.12.0):

1. marketplace ``Service`` wins on UUID collision (priority);
2. otherwise ``SalonService`` — but ONLY with an ACTIVE
   ``SpecialistService`` link to the chosen specialist in the current
   tenant.

Used identically by the internal slots path and CreateBookingService —
do NOT add a second resolution path elsewhere. The public mobile API is
deliberately NOT served by this resolver (AMD-019 bounds the change to
the internal surface).

Tenant isolation: the salon branch filters by tenant inside the query —
a service/link from another tenant is indistinguishable from a missing
one (no existence leak).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal
from uuid import UUID

logger = logging.getLogger(__name__)


class ServiceUnavailableForSpecialistError(Exception):
    """AMD-019 — the service does not exist, is inactive, or has no
    active SpecialistService link with the chosen specialist in the
    current tenant (another tenant's rows are the same error BY DESIGN —
    no existence leak)."""

    def __init__(self, service_id: UUID, specialist_id: UUID):
        self.service_id = service_id
        self.specialist_id = specialist_id
        super().__init__(
            f"Service {service_id} unavailable for specialist {specialist_id}"
        )


@dataclass(frozen=True)
class ResolvedService:
    """Unified resolution result — the fields the booking contour needs
    regardless of which catalog layer answered."""
    kind: Literal["marketplace", "salon"]
    service_id: UUID              # marketplace Service.id or SalonService.id
    name: str
    duration_minutes: int | None
    price: Decimal
    buffer_after_minutes: int = 0


def resolve_bookable_service(
    *,
    service_id: UUID,
    specialist,
    tenant=None,
) -> ResolvedService:
    """Resolve ``service_id`` for ``specialist`` per AMD-019.

    ``tenant`` is the booking tenant context (defaults to
    ``specialist.tenant`` — the same context CreateBookingService stamps
    onto the appointment). Raises ServiceUnavailableForSpecialistError —
    callers map it to their surface's existing "not found/inactive"
    shape (slots → 404, create → ServiceNotActiveError/422).
    """
    from services.models import SalonService, Service, SpecialistService

    # 1) Marketplace catalog — priority on UUID collision (AMD-019).
    service = (
        Service.objects
        .filter(id=service_id, specialist=specialist)
        .first()
    )
    if service is not None:
        if not service.is_active:
            raise ServiceUnavailableForSpecialistError(
                service_id, specialist.id,
            )
        return ResolvedService(
            kind="marketplace",
            service_id=service.id,
            name=service.name,
            duration_minutes=service.duration_minutes,
            price=service.price,
            buffer_after_minutes=service.buffer_after_minutes,
        )

    # 2) Salon catalog — only with an ACTIVE SpecialistService link in
    #    the current tenant. Tenant filter is INSIDE the query: another
    #    tenant's rows are invisible (no existence leak).
    tenant_id = (
        tenant.id if hasattr(tenant, "id")
        else tenant if tenant is not None
        else specialist.tenant_id
    )
    salon = (
        SalonService.objects
        .filter(id=service_id, is_active=True, tenant_id=tenant_id)
        .first()
    )
    if salon is None:
        raise ServiceUnavailableForSpecialistError(service_id, specialist.id)

    link = (
        SpecialistService.objects
        .filter(salon_service=salon, specialist=specialist, is_active=True)
        .first()
    )
    if link is None:
        raise ServiceUnavailableForSpecialistError(service_id, specialist.id)

    # Duration: SalonService.duration_minutes per AMD-019; the link's
    # resolution cascade (specialist → salon → template) is the fallback
    # for catalog rows whose duration is not yet curated (seed allows
    # nulls — an active bookable always resolves).
    duration = (
        salon.duration_minutes
        if salon.duration_minutes is not None
        else link.resolved_duration()
    )
    return ResolvedService(
        kind="salon",
        service_id=salon.id,
        name=salon.name,
        duration_minutes=duration,
        price=link.price,
        buffer_after_minutes=link.buffer_after_minutes,
    )
