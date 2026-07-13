"""S3C intake pipeline — normalized DTOs → S3A staging models.

Wires a source adapter (YClients API-pull today, CSV later) into the
S3A domain:

- services → ``DraftSalonService`` (staging, human confirms later in PR3).
  Idempotent upsert keyed by (tenant, external_source, external_service_id).
  Re-import updates the prefill fields but **preserves** a human
  confirm/reject status — a nightly re-pull must not resurrect a rejected
  draft or knock a confirmed one back to pending.
- staff → ``ExternalSourceMapping`` (external_type='staff'), resolving the
  YClients ``staff_id`` to an existing ``SpecialistProfile`` via
  ``yclients_staff_id``. Idempotent by the mapping's unique key.

Service→SalonService mapping is created at *confirm* time (PR3), when the
SalonService row actually materializes.

``tenant`` is passed in by the caller (the PR3 mgmt command resolves it
from the YClients company id); the pipeline stays tenant-explicit so it is
trivially testable and never guesses which salon it writes to.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from services.integrations.yclients.dto import RawServiceRecord, RawStaffRecord
from services.models import DraftSalonService, ExternalSourceMapping
from users.models import SpecialistProfile

logger = logging.getLogger("services.integrations.intake")

_NAME_MAX = 200  # DraftSalonService.external_name max_length


@dataclass
class IntakeSummary:
    """Counters returned by :func:`import_catalog` for observability."""

    services_created: int = 0
    services_updated: int = 0
    services_skipped: int = 0
    staff_mapped: int = 0
    staff_unmatched: int = 0


def upsert_service_draft(tenant, rec: RawServiceRecord) -> str:
    """Idempotent upsert of one service draft. Returns the outcome.

    Outcome ∈ {"created", "updated", "skipped"}. A record without an
    external id can't be keyed idempotently → skipped (the caller logs it).
    """
    eid = (rec.external_service_id or "").strip()
    if not eid:
        logger.warning(
            "intake.service_skipped reason=no_external_id tenant=%s name=%r",
            tenant.pk, rec.name[:_NAME_MAX],
        )
        return "skipped"

    existing = DraftSalonService.objects.filter(
        tenant=tenant,
        external_source=DraftSalonService.ExternalSource.YCLIENTS,
        external_service_id=eid,
    ).first()

    if existing is not None:
        # Update prefill fields only — status is human-owned, leave it.
        existing.external_name = rec.name[:_NAME_MAX]
        existing.suggested_duration = rec.duration_min
        existing.suggested_price = rec.price_min
        existing.raw_payload = rec.raw
        existing.save(update_fields=[
            "external_name", "suggested_duration", "suggested_price",
            "raw_payload", "updated_at",
        ])
        return "updated"

    DraftSalonService.objects.create(
        tenant=tenant,
        external_source=DraftSalonService.ExternalSource.YCLIENTS,
        external_service_id=eid,
        external_name=rec.name[:_NAME_MAX],
        suggested_duration=rec.duration_min,
        suggested_price=rec.price_min,
        raw_payload=rec.raw,
    )
    return "created"


def map_staff(tenant, st: RawStaffRecord) -> bool:
    """Idempotent staff→specialist mapping. Returns True if matched.

    Resolves the YClients ``staff_id`` to a ``SpecialistProfile`` already
    stamped with ``yclients_staff_id`` for this tenant. No match → the
    specialist isn't onboarded in Ayla yet; we count it and move on rather
    than inventing a row.
    """
    eid = (st.external_staff_id or "").strip()
    if not eid:
        return False

    specialist = SpecialistProfile.objects.filter(
        tenant=tenant, yclients_staff_id=eid,
    ).first()
    if specialist is None:
        logger.info(
            "intake.staff_unmatched tenant=%s yclients_staff_id=%s",
            tenant.pk, eid,
        )
        return False

    ExternalSourceMapping.objects.update_or_create(
        source=ExternalSourceMapping.Source.YCLIENTS,
        external_type=ExternalSourceMapping.ExternalType.STAFF,
        external_id=eid,
        tenant=tenant,
        defaults={"specialist": specialist, "salon_service": None},
    )
    return True


def import_catalog(source, tenant) -> IntakeSummary:
    """Pull from ``source`` and upsert drafts + staff mappings for ``tenant``.

    Idempotent: safe to run repeatedly (nightly / manual). Returns an
    :class:`IntakeSummary` of what changed.
    """
    summary = IntakeSummary()

    for rec in source.fetch_services():
        outcome = upsert_service_draft(tenant, rec)
        if outcome == "created":
            summary.services_created += 1
        elif outcome == "updated":
            summary.services_updated += 1
        else:
            summary.services_skipped += 1

    for st in source.fetch_staff():
        if map_staff(tenant, st):
            summary.staff_mapped += 1
        else:
            summary.staff_unmatched += 1

    logger.info(
        "intake.import_complete tenant=%s created=%d updated=%d skipped=%d "
        "staff_mapped=%d staff_unmatched=%d",
        tenant.pk, summary.services_created, summary.services_updated,
        summary.services_skipped, summary.staff_mapped, summary.staff_unmatched,
    )
    return summary
