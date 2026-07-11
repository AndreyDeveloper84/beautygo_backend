"""Normalized DTOs for YClients intake — source-agnostic.

An intake source (YClients API-pull or CSV bootstrap) yields these
records; the pipeline maps them onto S3A ``DraftSalonService`` /
``ExternalSourceMapping`` in a later chunk. Keeping them plain
dataclasses (not model instances) means the pipeline is testable
without a DB and without importing S3A models.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True)
class RawServiceRecord:
    """One normalized bookable service, ready to map to a DraftSalonService.

    Fields mirror ``DraftSalonService`` intake columns:
    ``external_service_id`` → external_service_id, ``name`` → external_name,
    ``duration_min`` → suggested_duration, ``price_min`` → suggested_price.
    ``raw`` keeps the untouched source payload for audit / re-mapping.
    """

    external_service_id: str
    name: str
    duration_min: int | None = None
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    category_id: str | None = None
    external_staff_ids: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RawStaffRecord:
    """One normalized specialist, ready to map to a SpecialistProfile.

    ``external_staff_id`` keys ``ExternalSourceMapping(external_type='staff')``
    and matches ``SpecialistProfile.yclients_staff_id``.
    """

    external_staff_id: str
    name: str
    specialization: str = ""
    bookable: bool = True
    raw: dict = field(default_factory=dict)
