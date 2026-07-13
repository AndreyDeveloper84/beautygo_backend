"""S3C — confirm a DraftSalonService into bookable catalog rows.

The human-in-the-loop step of intake: a pending draft becomes a real
``SalonService`` (mid layer) plus, for each performing specialist, a
``SpecialistService`` (the bookable unit). Idempotent:

- SalonService is reused via ``ExternalSourceMapping(external_type='service')``
  keyed by the YClients ``service_id`` — re-confirm updates, never duplicates.
- SpecialistService is upserted by its unique (specialist, salon_service).

Rules:
- A rejected draft is never confirmed.
- Off-taxonomy drafts (no ``suggested_template``) need a fallback category —
  ``SalonService.clean()`` requires template OR category.
- A bookable SpecialistService needs a price; a draft without
  ``suggested_price`` yields a SalonService but no bookable rows (reported).
- Staff to link come from ``staff_ids`` (explicit) or the draft's
  ``raw_payload['staff_ids']`` fallback; each resolves to a
  ``SpecialistProfile`` via ``yclients_staff_id`` (tenant-scoped).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from services.models import (
    ExternalSourceMapping,
    SalonService,
    SpecialistService,
)
from users.models import SpecialistProfile

logger = logging.getLogger("services.integrations.intake")


class DraftNotConfirmable(Exception):
    """The draft cannot be confirmed (rejected, or missing taxonomy)."""


@dataclass
class ConfirmResult:
    salon_service_id: str
    created_salon_service: bool
    specialist_services_created: int = 0
    specialist_services_skipped_no_price: int = 0
    specialist_services_skipped_invalid: int = 0
    unmatched_staff: list[str] = field(default_factory=list)


def _resolve_staff_ids(draft, staff_ids) -> list[str]:
    """Explicit ``staff_ids`` win; else the draft's raw_payload fallback."""
    if staff_ids is not None:
        source = staff_ids
    else:
        raw = draft.raw_payload or {}
        source = raw.get("staff_ids") or []
    return [str(s).strip() for s in source if str(s).strip()]


def _get_or_create_salon_service(draft, fallback_category):
    """Reuse the mapped SalonService or create one. Returns (salon, created)."""
    mapping = ExternalSourceMapping.objects.filter(
        source=ExternalSourceMapping.Source.YCLIENTS,
        external_type=ExternalSourceMapping.ExternalType.SERVICE,
        external_id=draft.external_service_id,
        tenant=draft.tenant,
    ).first()

    template = draft.suggested_template
    category = None if template is not None else fallback_category
    if template is None and category is None:
        raise DraftNotConfirmable(
            f"Draft {draft.pk} has no suggested_template; a fallback category "
            "is required to create an off-taxonomy SalonService."
        )

    if mapping is not None:
        salon = mapping.salon_service
        salon.name = draft.external_name
        salon.template = template
        salon.category = category
        salon.duration_minutes = draft.suggested_duration
        salon.base_price = draft.suggested_price
        salon.source = SalonService.Source.YCLIENTS
        salon.save()
        return salon, False

    try:
        salon = SalonService.objects.create(
            tenant=draft.tenant,
            template=template,
            category=category,
            name=draft.external_name,
            duration_minutes=draft.suggested_duration,
            base_price=draft.suggested_price,
            source=SalonService.Source.YCLIENTS,
        )
    except IntegrityError as exc:
        # SalonService unique (tenant, template, name): another service (a
        # different YClients id) already owns this template+name. Seed-safe:
        # surface as DraftNotConfirmable so the caller skips this draft rather
        # than crashing the whole intake run.
        raise DraftNotConfirmable(
            f"Draft {draft.pk} collides with an existing SalonService "
            f"(tenant, template, name={draft.external_name!r}): {exc}"
        ) from exc
    ExternalSourceMapping.objects.create(
        source=ExternalSourceMapping.Source.YCLIENTS,
        external_type=ExternalSourceMapping.ExternalType.SERVICE,
        external_id=draft.external_service_id,
        tenant=draft.tenant,
        salon_service=salon,
    )
    return salon, True


@transaction.atomic
def confirm_draft(draft, *, staff_ids=None, fallback_category=None, actor=None) -> ConfirmResult:
    """Materialize ``draft`` into a SalonService (+ bookable SpecialistServices).

    ``staff_ids`` — YClients staff ids to make the service bookable. When
    omitted, falls back to ``draft.raw_payload['staff_ids']``.
    """
    if draft.status == draft.Status.REJECTED:
        raise DraftNotConfirmable(f"Draft {draft.pk} is rejected.")

    salon, created = _get_or_create_salon_service(draft, fallback_category)

    result = ConfirmResult(
        salon_service_id=str(salon.pk), created_salon_service=created,
    )

    for staff_id in _resolve_staff_ids(draft, staff_ids):
        specialist = SpecialistProfile.objects.filter(
            tenant=draft.tenant, yclients_staff_id=staff_id,
        ).first()
        if specialist is None:
            result.unmatched_staff.append(staff_id)
            continue
        if draft.suggested_price is None:
            # SpecialistService.price is required (>=1) — can't make it
            # bookable without a price. Report, don't guess a value.
            result.specialist_services_skipped_no_price += 1
            continue
        try:
            _, ss_created = SpecialistService.objects.update_or_create(
                specialist=specialist,
                salon_service=salon,
                defaults={
                    "tenant": draft.tenant,
                    "price": draft.suggested_price,
                    "duration_minutes": draft.suggested_duration,
                    "is_active": True,
                },
            )
        except ValidationError:
            # No resolvable duration (off-taxonomy draft, no duration, no
            # template default) → an active bookable row is invalid. clean()
            # rejects it before any DB write, so the transaction stays intact:
            # report and keep confirming the rest rather than crashing the seed.
            result.specialist_services_skipped_invalid += 1
            continue
        if ss_created:
            result.specialist_services_created += 1

    draft.status = draft.Status.CONFIRMED
    draft.confirmed_salon_service = salon
    draft.confirmed_at = timezone.now()
    draft.confirmed_by = actor
    draft.save(update_fields=[
        "status", "confirmed_salon_service", "confirmed_at",
        "confirmed_by", "updated_at",
    ])

    logger.info(
        "intake.confirm draft=%s salon_service=%s created=%s "
        "bookable=%d skipped_no_price=%d unmatched=%d",
        draft.pk, salon.pk, created, result.specialist_services_created,
        result.specialist_services_skipped_no_price, len(result.unmatched_staff),
    )
    return result
