"""Выбор канонических услуг мастером-соло (DRF-1800, M8a; макеты 3.x, P15/P18).

Мастер на экране «Выберите услуги» отмечает шаблоны канона; этот модуль
превращает выбор в строки каталога. Решения, из которых он сложен:

* **При выборе создаётся только ``SalonService``** — строка «эта услуга есть
  в моём workspace»: ``template`` и ``category`` из канона, ``name`` =
  имя шаблона, ``base_price`` пуст (поле и так nullable), статус связи
  ``VERIFIED``, подтверждённый **правилом** (решение владельца §77 п.30 от
  24.09, DRF-2406). Что правило подтверждает и чего НЕ подтверждает —
  в ``MASTER_SELECT_RULE`` ниже; предел там записан намеренно.
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
#: По этому префиксу правило ниже и узнаёт свои строки — и только свои.
SOURCE_REF_PREFIX = "master_select:"

#: Имя правила, подтверждающего связь при выборе мастера (§77 п.30, DRF-2406).
#:
#: **Что правило подтверждает:** мастер **выбрал** эту услугу из канона.
#: Источник связи — его собственное действие на экране «Выберите услуги», и
#: `mapping_source_ref` хранит, кто именно выбрал.
#:
#: **Чего правило НЕ подтверждает — и это предел, а не недоработка:** что
#: мастер эту услугу **оказывает**. Прежняя схема ставила сюда человека, и
#: ровно этот вопрос он должен был проверять. Владелец принял риск сознательно:
#: подтверждать было некому — в группе прав ноль человек, и очередь на
#: подтверждение росла вечно по построению, то есть «проверка» существовала
#: только на бумаге.
#:
#: **Откат статуса решением этого листа не вводится.** Формулировка владельца
#: в докстринге `MappingStatus` говорит у́же, чем хочется процитировать: «ноль
#: `VERIFIED` не разрешает откат на `REVIEW_REQUIRED`» — отвергнут конкретный
#: довод, а не объявлен общий запрет. Здесь опора не на неё, а на §77 п.30: раз
#: подтверждать некому, возврат в очередь вернул бы ровно то состояние, ради
#: выхода из которого решение и принято. Если понадобится выборочный контроль,
#: он вводится **поверх** — отдельным признаком или отдельным разбором.
#:
#: Формулировка имени намеренно описывает ровно произошедшее и не присваивает
#: себе проверку человеком; это сторожится узлом
#: `test_rule_name_does_not_claim_the_master_performs_it` — усилить
#: формулировку при следующей правке ничего не стоит, а проверить потом будет
#: нечем. Тот же приём уже применён к `grandfathered_before_lifecycle`.
MASTER_SELECT_RULE = "master_selected_from_canon"
#: Версия правила. Меняется, когда меняется само основание подтверждения, а не
#: когда правят код вокруг: «подтверждено правилом» без версии неотличимо от
#: «подтверждено какой-то из его версий».
MASTER_SELECT_RULE_VERSION = "1"


def rule_confirmation(*, source_ref: str, at=None) -> dict[str, object]:
    """Провенанс подтверждения правилом целиком: правило, версия, дата, основание.

    Одно место на живой выбор и на разовый прогон по уже существующим строкам:
    иначе два вызова однажды разойдутся, и половина строк окажется подтверждена
    «каким-то правилом без версии».

    `source_ref` **обязателен и возвращается отсюда же**, хотя значение у каждой
    строки своё. Прежняя редакция оставляла его вызывающему — и тогда второй
    вызывающий, забывший его подставить, упирался бы в
    `salonservice_verified_requires_provenance` уже на записи. Ровно тот же
    довод, по которому проверка стоит в схеме, а не в `clean()`: пусть
    нарушение будет **невозможно построить**, а не «замечено на ревью».

    Поля не «на всякий случай»: без даты, автора и основания схема `VERIFIED`
    не примет — статус без происхождения через месяц читается как умолчание.
    """

    if not source_ref:
        raise ValueError("подтверждение правилом без основания: source_ref пуст")
    return {
        "mapping_status": SalonService.MappingStatus.VERIFIED,
        "mapping_confirmed_rule": MASTER_SELECT_RULE,
        "mapping_rule_version": MASTER_SELECT_RULE_VERSION,
        "mapping_confirmed_at": at or timezone.now(),
        "mapping_source_ref": source_ref,
    }


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
                        **rule_confirmation(
                            source_ref=f"{SOURCE_REF_PREFIX}{profile.pk}"
                        ),
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

#: Статусы, при которых связь считается решённой. Одного статуса для удаления
#: уже НЕ хватает: с DRF-2406 `VERIFIED` ставит и правило выбора мастера, то
#: есть решённой стала бы каждая выбранная строка. Кто именно решил — разбирает
#: `decided_by_someone_else` ниже; этот набор отвечает только на вопрос
#: «решение вообще есть?».
DECIDED_MAPPING = frozenset({
    SalonService.MappingStatus.VERIFIED,
    SalonService.MappingStatus.NOT_RECOMMENDABLE,
})


def decided_by_someone_else(row: SalonService) -> bool:
    """Держит ли строку **чужое** решение о связи — не собственный выбор мастера.

    До DRF-2406 хватало одного статуса: строку с решённой связью удаление не
    стирало. Теперь решённой становится **каждая** выбранная строка, и кнопка
    «Убрать из моих услуг» перестала бы удалять что-либо вовсе: мастер, убравший
    услугу сразу после выбора, оставлял бы выключенную строку навсегда. Это было
    бы **молчаливым** изменением — лист §77 п.30 менял очередь на подтверждение,
    а не смысл кнопки.

    Граница проходит не между человеком и правилом: подтверждать связь правилом
    умеет не только выбор мастера — в коде это делают `map_salon_services:R*`
    (`mapping_apply`), `tech_tenant_fixture`, `golden_stand`, — и такие решения
    удаление по-прежнему не стирает. Граница — **чьё это решение**:

    * решение человека или другого правила — чужое, строку держим;
    * подтверждение собственным выбором мастера — не чужое: убрать услугу
      значит отозвать ровно то действие, которым строка и появилась.

    Так поведение кнопки остаётся прежним для всех, кого она касалась раньше.
    """

    if row.mapping_status not in DECIDED_MAPPING:
        return False
    if row.mapping_status == SalonService.MappingStatus.NOT_RECOMMENDABLE:
        # Отказ правило выбора не ставит никогда: такой статус — всегда чужое
        # решение, кто бы ни значился в провенансе.
        return True
    return row.mapping_confirmed_rule != MASTER_SELECT_RULE


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
      **кем-то другим** (DRF-2406: собственное подтверждение выбора мастер
      вправе отозвать, чужое решение удаление не стирает). Иначе строка и
      предложения выключаются: история записей и чужая разметка остаются.
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

    keep = decided_by_someone_else(row) or bookings.exists()
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
    "MASTER_SELECT_RULE",
    "MASTER_SELECT_RULE_VERSION",
    "MAX_TEMPLATES_PER_CALL",
    "SOURCE_REF_PREFIX",
    "HasFutureAppointments",
    "SelectedService",
    "SelectionRefused",
    "ServiceNotSelected",
    "TemplatesNotFound",
    "decided_by_someone_else",
    "remove_service",
    "rule_confirmation",
    "select_templates",
    "selected_services",
    "set_offer",
    "workspace_tenant",
]
