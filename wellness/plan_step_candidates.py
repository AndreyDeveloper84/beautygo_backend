"""Plan Engine — кандидаты услуги для шага плана (DRF-2868, контракт §8.2).

Шаг плана несёт способность, а не услугу. Довести его до услуги салона можно
только через подбор: шаг не получает услугу в обход резолвера (§8.2). Здесь —
этот путь, целиком на стороне каталога:

    способность шага
      → предложения салонов в видимости ЭТОГО клиента   (users.capability_offers)
      → допуск предложения, восемь проверок             (users.admission)
      → происхождение ответа о проверке здоровья        (SpecialistService)

Три решения владельца, которые здесь исполняются:

* 07.10 (S2): неподтверждённое «проверка здоровья не нужна» — «неизвестно», и
  неподтверждённое «нужна» — «требование не подтверждено». Мастер, у которого
  ответ о здоровье по этой услуге не подтверждён, кандидатом не считается:
  условия услуги не определены, и это вопрос к данным услуги, не к клиенту;
* 08.10: услуга, у которой проверка допуска «не действует» (обход выключенного
  флага каталога), в услугу шага не идёт — см. :data:`NOT_ENFORCED_IS_UNMET`.
  Сохранению общей части плана это не мешает: слои раздельны;
* 08.10: что мешает подобрать услугу, называется раздельно — закрытым списком
  причин, а не одним «не нашла».

Каталог кандидатов не ранжирует и «лучшего» не выбирает: порядок устойчивый,
выбор — человека. Ничего не пишет. Гейт здоровья самой записи остаётся на
записи и отсюда не снимается: ``health_check = required`` у кандидата значит,
что запись встретит расспрос.
"""
from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from recommendation.api import first_unmet
from services.models import SalonService, SpecialistService
from services.synthetic import SyntheticGrant, grant_for
from tenants.distance import offer_address
from users.admission import OfferVerdict, offer_admission
from users.capability_offers import offers_by_capability

from .models import Plan, PlanRevision

#: Решение владельца 08.10 (вопрос (c) матрицы проверок): шаг плана требует
#: ПРОВЕРЕННОГО допуска. Проверка, которая «не действует», для шага — не
#: сошедшаяся, с её собственным исходом «не проверялось», а не «не прошла».
NOT_ENFORCED_IS_UNMET = True

# Почему у шага нет ни одного кандидата — закрытый список.
NO_CAPABILITY = "NO_CAPABILITY"
NO_OFFER = "NO_OFFER"
OUT_OF_SIGHT = "OUT_OF_SIGHT"
NO_SELLABLE_MASTER = "NO_SELLABLE_MASTER"
NOT_ADMITTED = "NOT_ADMITTED"
HEALTH_CONDITIONS_UNDEFINED = "HEALTH_CONDITIONS_UNDEFINED"
#: Отклонены все, и по разным причинам.
NONE_ADMITTED = "NONE_ADMITTED"

HEALTH_REQUIRED = "required"
HEALTH_NOT_REQUIRED = "not_required"


def _capability_of(revision: PlanRevision, step_id: str) -> str:
    step = next(s for s in revision.steps_snapshot if s.get("step_id") == step_id)
    return step["capability_ref"]


def _masters_with_defined_health(
    offer_id: UUID, master_ids: list[UUID], *, include_synthetic: SyntheticGrant | None,
) -> list[tuple[SpecialistService, bool]]:
    """Мастера, у которых ответ о проверке здоровья по этой услуге ПОДТВЕРЖДЁН.

    Возвращает пары «ребро, нужен ли расспрос». Неподтверждённый ответ (в любую
    сторону) и отсутствие ответа — мастер не возвращается.

    ``include_synthetic`` — серверное разрешение ЭТОГО человека: ответ
    синтетической услуги подтверждён синтетическим правилом и считается
    подтверждённым только под ним. Настоящих услуг оно не касается.
    """
    out: list[tuple[SpecialistService, bool]] = []
    edges = (
        SpecialistService.objects.filter(salon_service_id=offer_id, specialist_id__in=master_ids)
        .select_related("specialist", "specialist__works_at", "salon_service", "salon_service__template")
        .order_by("specialist_id")
    )
    for edge in edges:
        verdict, _basis, confirmed = edge.resolved_health_check_with_origin(include_synthetic=include_synthetic)
        if confirmed and verdict is not None:
            out.append((edge, bool(verdict)))
    return out


def _display(offer: SalonService, masters: list[tuple[SpecialistService, bool]]) -> dict[str, Any]:
    """То, что человек увидит, — словами каталога: те же поля, что у карточки
    предложения. Ничего сверх них.

    Адрес — места, где мастер оказывает услугу, и только подтверждённого
    (§9, ``offer_address``): старые адреса салона и профиля клиенту не
    называются. ``None`` — адрес не подтверждён, а не «адреса нет»."""
    tenant = offer.tenant
    return {
        "service_name": offer.name,
        "salon": {"name": tenant.name, "city": tenant.city or None},
        "masters": [
            {
                "specialist_ref": str(edge.specialist_id),
                "name": edge.specialist.display_name,
                "price": str(edge.price),
                "duration_minutes": edge.resolved_duration(),
                "place_address": offer_address(edge.specialist) or None,
            }
            for edge, _ in masters
        ],
    }


def step_candidates(user, plan: Plan, step_id: str) -> dict[str, Any]:
    """Кандидаты услуги для шага. Допуск самого шага (план действует, вердикт
    хода, ограничения) вызывающий проверяет ДО — здесь только подбор."""
    capability_ref = _capability_of(plan.current_revision, step_id)
    found = offers_by_capability(capability_ref, viewer=user)
    rejected = {NO_SELLABLE_MASTER: 0, NOT_ADMITTED: 0, HEALTH_CONDITIONS_UNDEFINED: 0}
    candidates: list[dict[str, Any]] = []

    if found.offer_ids:
        grant = grant_for(user)
        admission = offer_admission(found.offer_ids, viewer=user, not_enforced_is_unmet=NOT_ENFORCED_IS_UNMET)
        offers = {
            o.pk: o
            for o in SalonService.objects.filter(pk__in=found.offer_ids).select_related("tenant")
        }
        for offer_id in found.offer_ids:
            verdict = admission.get(offer_id)
            offer = offers.get(offer_id)
            if verdict is None or offer is None or verdict.verdict is OfferVerdict.NOT_ADMITTED:
                rejected[NOT_ADMITTED] += 1
                continue
            if verdict.verdict is OfferVerdict.NO_SELLABLE_MASTER:
                rejected[NO_SELLABLE_MASTER] += 1
                continue
            # Вердикт «открыто» — есть хотя бы один прошедший мастер; какие
            # именно — читается той же строгой свёрткой по каждому.
            admitted = [
                master_id
                for master_id, answers in verdict.masters.items()
                if first_unmet(answers, not_enforced_is_unmet=NOT_ENFORCED_IS_UNMET) is None
            ]
            masters = _masters_with_defined_health(offer_id, admitted, include_synthetic=grant)
            if not masters:
                rejected[HEALTH_CONDITIONS_UNDEFINED] += 1
                continue
            candidates.append(
                {
                    "tenant_offer_ref": str(offer_id),
                    "canonical_service_ref": str(offer.template_id),
                    "health_check": HEALTH_REQUIRED if any(req for _, req in masters) else HEALTH_NOT_REQUIRED,
                    "display": _display(offer, masters),
                    # Помеченные синтетические данные, прочитанные под разрешением
                    # этого человека (DRF-2916): вызывающий обязан показать это
                    # словами. У настоящего предложения всегда ``False``.
                    "synthetic": verdict.synthetic,
                }
            )

    return {
        "step_id": step_id,
        "capability_ref": capability_ref,
        "candidates": candidates,
        # Идентификатор этого поиска: вызывающий возвращает его в переходе
        # шага как ``resolver_decision_id``. Каталог его не хранит — при
        # выборе кандидат перепроверяется заново, а не по памяти.
        "search_id": str(uuid.uuid4()),
        "nothing_because": None if candidates else _nothing_because(found.empty_because, rejected),
        "rejected": rejected,
    }


def _nothing_because(empty_because, rejected: dict[str, int]) -> str:
    """Одна причина, когда она одна; ``NONE_ADMITTED`` — только для смеси."""
    if empty_because is not None:
        return empty_because.value
    named = [reason for reason, count in rejected.items() if count]
    return named[0] if len(named) == 1 else NONE_ADMITTED


def offer_is_candidate(user, plan: Plan, step_id: str, tenant_offer_ref: UUID) -> bool:
    """Является ли предложение допущенным кандидатом шага ПРЯМО СЕЙЧАС.

    Читается заново при каждом выборе: довести шаг до услуги в обход подбора
    нельзя, и вчерашний показ кандидата сегодня ничего не гарантирует."""
    wanted = str(tenant_offer_ref)
    return any(c["tenant_offer_ref"] == wanted for c in step_candidates(user, plan, step_id)["candidates"])


__all__ = [
    "HEALTH_CONDITIONS_UNDEFINED",
    "NONE_ADMITTED",
    "NOT_ENFORCED_IS_UNMET",
    "offer_is_candidate",
    "step_candidates",
]
