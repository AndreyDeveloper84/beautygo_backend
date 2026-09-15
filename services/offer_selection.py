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
  связи, имя и активность не трогаются (решённую разметку модератора
  выбор мастера не переписывает; см. DRF-1668 в ``intake/confirm.py``).
* **Всё или ничего.** Хоть один неизвестный шаблон — ``TemplatesNotFound``,
  ничего не создано. Шаблон из категории чужого тенанта — тоже неизвестный.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from django.db import IntegrityError, transaction
from django.db.models import Q

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
            if _existing(tenant, template) is not None:
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
        .select_related("template", "category")
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


__all__ = [
    "MAX_TEMPLATES_PER_CALL",
    "SelectedService",
    "SelectionRefused",
    "TemplatesNotFound",
    "select_templates",
    "selected_services",
    "workspace_tenant",
]
