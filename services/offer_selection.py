"""Выбор канонических услуг мастером-соло (DRF-1800, M8a; макеты 3.x, P15/P18).

Мастер на экране «Выберите услуги» отмечает шаблоны канона; этот модуль
превращает выбор в строки каталога. Решения, из которых он сложен:

* **При выборе создаётся только ``SalonService``** — строка «эта услуга есть
  в моём workspace»: ``template`` и ``category`` из канона, ``name`` =
  имя шаблона, ``base_price`` пуст (поле и так nullable), статус связи
  ``REVIEW_REQUIRED`` (G5 → а: связь выбрана человеком, но не
  подтверждена модератором; в подбор — только после ``VERIFIED``).
* **``SpecialistService`` при выборе НЕ создаётся.** Его ``price`` NOT
  NULL, и у поля 18 читателей в 11 файлах, включая запись (``Decimal(None)``
  в ``create_booking_service``) и зеркало бота
  ``/internal/catalog/specialist-services/``, у которого гейта по
  ``is_active`` нет вовсе. Строка предложения появляется при первой цене
  (M8b) — так же, как ``intake/confirm.py`` пропускает её без цены.
  «Выбрано» = активная ``SalonService`` с шаблоном; «настроено» = у неё есть
  активное предложение этого мастера с определимой длительностью.
* **Только соло.** Каталог салона ведёт владелец салона: мастер салона,
  выбирающий услуги, менял бы список всего салона. Отказ —
  ``salon_catalog_owner_managed``.
* **Идемпотентно по (tenant, template), без перепривязки.** Уже
  существующая строка с этим шаблоном возвращается как есть — её статус
  связи и имя не трогаются; выключенная строка включается снова (решённую разметку модератора
  выбор мастера не переписывает; см. DRF-1668 в ``intake/confirm.py``).
* **Всё или ничего.** Хоть один неизвестный шаблон — ``TemplatesNotFound``,
  ничего не создано. Шаблон из категории чужого тенанта — тоже неизвестный.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import ProtectedError, Q
from django.utils import timezone

from tenants.models import Tenant

from .models import SalonService, ServiceTemplate, SpecialistService

logger = logging.getLogger(__name__)

#: Сколько шаблонов принимает один вызов. На экране — одно направление за раз.
MAX_TEMPLATES_PER_CALL = 200
#: ``SalonService.mapping_source_ref`` строки, заведённой выбором мастера.
SOURCE_REF_PREFIX = "master_select:"


class SelectionRefused(Exception):
    """Отказ с машинной причиной — 409 наружу, ничего не создано."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class TemplatesNotFound(Exception):
    """Шаблонов с такими id нет (или они в категории чужого тенанта)."""

    def __init__(self, template_ids: list[str]) -> None:
        super().__init__("template_not_found")
        self.template_ids = template_ids


@dataclass(frozen=True)
class SelectedService:
    salon_service: SalonService
    offer: SpecialistService | None

    @property
    def configured(self) -> bool:
        offer = self.offer
        return (
            self.salon_service.is_active
            and offer is not None
            and offer.is_active
            and offer.resolved_duration() is not None
        )


def workspace_tenant(profile) -> Tenant:
    """Тенант, в котором мастер вправе выбирать услуги, или отказ."""

    tenant = profile.tenant
    if tenant is None:
        raise SelectionRefused("no_workspace_tenant")
    if tenant.kind != Tenant.Kind.SOLO:
        raise SelectionRefused("salon_catalog_owner_managed")
    return tenant


def select_templates(profile, template_ids: Iterable[UUID]) -> int:
    """Завести ``SalonService`` на каждый ещё не выбранный шаблон. Возвращает число созданных."""

    tenant = workspace_tenant(profile)
    wanted = list(dict.fromkeys(template_ids))
    templates = {
        tpl.pk: tpl
        for tpl in ServiceTemplate.objects.select_related("category").filter(
            Q(category__tenant__isnull=True) | Q(category__tenant=tenant),
            pk__in=wanted,
        )
    }
    missing = [str(tid) for tid in wanted if tid not in templates]
    if missing:
        raise TemplatesNotFound(missing)

    created = 0
    with transaction.atomic():
        for tid in wanted:
            template = templates[tid]
            existing = _existing(tenant, template)
            if existing is not None:
                if not existing.is_active:
                    # Мастер убрал услугу (M8b) и выбрал снова: связь и имя
                    # те же, включается только строка.
                    existing.is_active = True
                    existing.save(update_fields=["is_active", "updated_at"])
                    created += 1
                continue
            try:
                with transaction.atomic():
                    SalonService.objects.create(
                        tenant=tenant,
                        template=template,
                        category=template.category,
                        name=template.name,
                        source=SalonService.Source.MANUAL,
                        mapping_status=SalonService.MappingStatus.REVIEW_REQUIRED,
                        mapping_source_ref=f"{SOURCE_REF_PREFIX}{profile.pk}",
                    )
            except IntegrityError:
                # Гонка двух одинаковых вызовов: проигравший видит строку победителя.
                if _existing(tenant, template) is None:
                    raise
                continue
            created += 1

    logger.info(
        "services.offer_selection.selected specialist=%s tenant=%s requested=%s created=%s",
        profile.pk, tenant.pk, len(wanted), created,
    )
    return created


def selected_services(profile) -> list[SelectedService]:
    """Выбранные услуги workspace с предложением этого мастера, если оно есть."""

    tenant = workspace_tenant(profile)
    rows = list(
        SalonService.objects
        .select_related("template", "template__category", "category")
        .filter(tenant=tenant, template__isnull=False)
        .order_by("created_at", "name")
    )
    offers = {
        offer.salon_service_id: offer
        for offer in SpecialistService.objects.filter(specialist=profile, salon_service__in=rows)
    }
    result = []
    for row in rows:
        offer = offers.get(row.pk)
        if offer is not None:
            offer.salon_service = row  # resolved_duration без повторного запроса
        result.append(SelectedService(salon_service=row, offer=offer))
    return result


def _existing(tenant: Tenant, template: ServiceTemplate) -> SalonService | None:
    return (
        SalonService.objects
        .filter(tenant=tenant, template=template)
        .order_by("created_at")
        .first()
    )


# --- M8b: цена и длительность, «Убрать из моих услуг» -----------------------

#: Решённые модератором статусы связи: такую строку удаление не стирает.
DECIDED_MAPPING = frozenset({
    SalonService.MappingStatus.VERIFIED,
    SalonService.MappingStatus.NOT_RECOMMENDABLE,
})


class ServiceNotSelected(Exception):
    """Строки с таким id нет среди выбранных в workspace мастера."""


class HasFutureAppointments(Exception):
    """У услуги есть будущая живая запись — убрать нельзя."""

    def __init__(self, count: int) -> None:
        super().__init__("has_future_appointments")
        self.count = count


def _selected_row(profile, salon_service_id: UUID) -> SalonService:
    tenant = workspace_tenant(profile)
    row = (
        SalonService.objects
        .select_related("template", "tenant")
        .filter(pk=salon_service_id, tenant=tenant, template__isnull=False)
        .first()
    )
    if row is None:
        raise ServiceNotSelected(str(salon_service_id))
    return row


def set_offer(
    profile, salon_service_id: UUID, *, price: Decimal, duration_minutes: int,
) -> tuple[SpecialistService, bool]:
    """Первая цена создаёт предложение мастера; следующие — обновляют его.

    Возвращает ``(offer, created)``. Строка услуги, убранная мастером, —
    отказ ``service_removed``: сначала выбрать снова (выбор включит её).
    """

    row = _selected_row(profile, salon_service_id)
    if not row.is_active:
        raise SelectionRefused("service_removed")

    def _write(offer: SpecialistService) -> None:
        offer.price = price
        offer.duration_minutes = duration_minutes
        offer.is_active = True
        offer.save()

    with transaction.atomic():
        offer = (
            SpecialistService.objects.select_for_update()
            .filter(specialist=profile, salon_service=row)
            .first()
        )
        created = offer is None
        if offer is not None:
            _write(offer)
        else:
            try:
                with transaction.atomic():
                    offer = SpecialistService(specialist=profile, salon_service=row)
                    _write(offer)
            except IntegrityError:
                # Гонка двух первых цен: проигравший обновляет строку победителя.
                offer = SpecialistService.objects.select_for_update().get(
                    specialist=profile, salon_service=row,
                )
                _write(offer)
                created = False

    logger.info(
        "services.offer_selection.offer_set specialist=%s salon_service=%s offer=%s created=%s",
        profile.pk, row.pk, offer.pk, created,
    )
    return offer, created


def remove_service(profile, salon_service_id: UUID) -> str:
    """«Убрать из моих услуг». Возвращает ``deleted`` или ``deactivated``.

    * Будущая живая запись на услугу (статусы ``ACTIVE_BOOKING_STATUSES``,
      конец в будущем) — ``HasFutureAppointments``, ничего не тронуто.
    * Строка удаляется целиком, только если её ничто не держит: нет ни
      одной записи (``Appointment.salon_service`` — PROTECT) и связь не решена
      модератором. Иначе строка и предложения выключаются: история записей
      и разметка модератора остаются.
    """

    from appointments.domain.value_objects import ACTIVE_BOOKING_STATUSES
    from appointments.models import Appointment

    row = _selected_row(profile, salon_service_id)
    bookings = Appointment.objects.filter(salon_service=row)
    future = bookings.filter(
        status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
        end_datetime__gt=timezone.now(),
    ).count()
    if future:
        raise HasFutureAppointments(future)

    keep = row.mapping_status in DECIDED_MAPPING or bookings.exists()
    outcome = "deactivated"
    with transaction.atomic():
        if not keep:
            try:
                with transaction.atomic():
                    SpecialistService.objects.filter(salon_service=row).delete()
                    row.delete()
                outcome = "deleted"
            except ProtectedError:
                keep = True
        if keep:
            SpecialistService.objects.filter(salon_service=row).update(is_active=False)
            if row.is_active:
                row.is_active = False
                row.save(update_fields=["is_active", "updated_at"])

    logger.info(
        "services.offer_selection.removed specialist=%s salon_service=%s outcome=%s",
        profile.pk, salon_service_id, outcome,
    )
    return outcome


__all__ = [
    "MAX_TEMPLATES_PER_CALL",
    "HasFutureAppointments",
    "SelectedService",
    "SelectionRefused",
    "ServiceNotSelected",
    "TemplatesNotFound",
    "remove_service",
    "select_templates",
    "selected_services",
    "set_offer",
    "workspace_tenant",
]
