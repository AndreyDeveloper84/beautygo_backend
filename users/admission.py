"""Допуск каталога по строкам — один читатель для подбора, переписи и плана (DRF-2888).

Подбор судит о кандидате — мастере с отвечающей строкой. Перепись допуска в
каталоге и шаг плана судят о ПРЕДЛОЖЕНИИ салона. Правила у них обязаны быть
одни: DRF-2793 показал, что проверка, добавленная в подбор, до переписи сама
не доезжает. Поэтому здесь две функции поверх того же шва, которым
пользуется источник подбора (:mod:`users.recommendation_source`):

* :func:`admission_answers` — ответ каждой из восьми проверок по тройкам
  «мастер, предложение, канон»;
* :func:`offer_admission` — вердикт по предложению: открывает ли его хоть
  один продаваемый мастер, и если нет — почему.

Только чтение. Положение флага каталога — то, что действует в момент вызова.

Помеченная синтетика (DRF-2916) для этих функций не существует: синтетическое
предложение отвечает как id, которого нет в базе, — без личности, в
операторском режиме и для любого, у кого нет серверного разрешения.

Единственное исключение — субъект с действующим разрешением
(:func:`services.synthetic.grant_for`: флаг стенда, серверный список,
тестовая личность). Ему синтетическое предложение отдаётся с отдельным
исходом ``SYNTHETIC`` у проверки связи и признаком ``OfferAdmission.synthetic``.
Попросить синтетику параметром нельзя: разрешение выводится из ``viewer``.

Что эти функции НЕ отвечают: безопасность хода и здоровье, совпадение с
нуждой, бюджет, область запроса, согласия. Допуск каталога — необходимое
условие рекомендации и перехода шага плана к услуге, но не достаточное.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from django.db.models import F, Q

from recommendation.api import ALL_CHECKS, AdmissionCheck, CheckAnswer, ConfigGate, build_answers, first_unmet
from services import body_care_address, body_care_license, body_care_qualification
from services.models import SalonService, ServiceTemplate, SpecialistService
from services.offer_sellable import sellable_offer_q
from services.synthetic import grant_for, offers_reading_as_synthetic, real_offer_q
from users.models import SpecialistProfile
from users.recommendation_source import _MappingFacts, config_readiness, legal_answers, unenforced
from users.sellable import demo_scope_q, demo_visibility_q, sellable_q

_RETIRED = ServiceTemplate.Lifecycle.RETIRED

Row = tuple[UUID | None, UUID, UUID | None]
Key = tuple[UUID | None, UUID]


def admission_answers(rows: Iterable[Row], *, viewer=None) -> dict[Key, tuple[CheckAnswer, ...]]:
    """Ответ каждой проверки допуска — по тройкам ``(мастер | None, предложение, канон | None)``.

    Ключ ответа — ``(мастер | None, предложение)``; значение — восемь ответов
    в порядке :data:`recommendation.api.ALL_CHECKS`. Мастер ``None`` — вопрос
    про само предложение: проверки уровня мастера (адрес, квалификация)
    отвечают «не применима», остальные — как обычно.

    Число запросов постоянно и от размера списка не зависит. Предложение,
    которого нет в базе, в ответ не попадает — как у читателей каталога.

    ``viewer`` — пользователь каталога, для которого идёт вопрос. Нужен для
    одного: помеченная синтетика существует только для субъекта с серверным
    разрешением (:func:`services.synthetic.grant_for`). Для всех остальных и
    при ``viewer=None`` синтетическое предложение в ответ не попадает. Под
    разрешением — попадает, с исходом ``SYNTHETIC`` у проверки связи; пара с
    мастером ДРУГОГО салона не попадает и тогда: вся цепочка тестовая.
    """
    rows = list(dict.fromkeys(rows))
    if not rows:
        return {}
    asked = {salon_id for _, salon_id, _ in rows}
    as_synthetic = offers_reading_as_synthetic(asked, include_synthetic=grant_for(viewer))
    offers = {
        offer["pk"]: offer
        for offer in SalonService.objects.filter(
            real_offer_q() | Q(pk__in=as_synthetic), pk__in=asked,
        ).values(
            "pk", "tenant_id", "mapping_status", "template_id", "template__lifecycle",
            "template__body_care_scope", "template__service_family",
            "template__legal_service_class", "template__required_practitioner_class",
        )
    }
    known = [row for row in rows if row[1] in offers]
    if as_synthetic:
        known = _only_masters_of_the_same_salon(known, offers, as_synthetic)
    readiness = config_readiness(offers)
    legal = legal_answers(known)

    out: dict[Key, tuple[CheckAnswer, ...]] = {}
    for specialist_id, salon_id, _ in known:
        offer = offers[salon_id]
        has_canon = offer["template_id"] is not None
        answers = legal[(specialist_id, salon_id)]
        out[(specialist_id, salon_id)] = build_answers(
            mapping_status=_MappingFacts._as_status(offer["mapping_status"]),
            canon_retired=(offer["template__lifecycle"] == _RETIRED) if has_canon else None,
            config_gate=readiness.get(salon_id, ConfigGate.UNDETERMINED),
            license_gate=answers.license,
            address_gate=answers.address,
            qualification_gate=answers.qualification,
            has_canon=has_canon,
            has_master=specialist_id is not None,
            license_verified=answers.license_raw == body_care_license.VERIFIED,
            address_verified=answers.address_raw == body_care_address.VERIFIED,
            qualification_verified=answers.qualification_raw == body_care_qualification.VERIFIED,
            address_waits_for_license=answers.address_raw == body_care_address.NO_COVERING_LICENSE,
            unenforced=unenforced(
                has_canon=has_canon,
                scope=offer["template__body_care_scope"],
                family=offer["template__service_family"],
                legal_class=offer["template__legal_service_class"],
                required_practitioner_class=offer["template__required_practitioner_class"],
            ),
            synthetic=salon_id in as_synthetic,
        )
    return out


def _only_masters_of_the_same_salon(rows: list[Row], offers: dict, as_synthetic: frozenset) -> list[Row]:
    """У синтетического предложения — только мастера его же салона.

    Замок базы проверяет это при записи предложения мастера; мастера могли
    перевести в другой салон позже. Тестовая личность видит и настоящие
    салоны, так что без этого условия тестовая запись ушла бы в чужой слот.
    """
    masters = {specialist_id for specialist_id, salon_id, _ in rows if specialist_id and salon_id in as_synthetic}
    if not masters:
        return rows
    salon_of = dict(SpecialistProfile.objects.filter(pk__in=masters).values_list("pk", "tenant_id"))
    return [
        row for row in rows
        if row[0] is None or row[1] not in as_synthetic or salon_of.get(row[0]) == offers[row[1]]["tenant_id"]
    ]


class OfferVerdict(StrEnum):
    #: Есть продаваемый мастер, у которого ни одна проверка не осталась несошедшейся.
    OPEN = "OPEN"
    #: У предложения нет ни одного продаваемого мастера. Это не отказ проверки
    #: допуска: «некому оказать услугу» и «услуга не допущена» — разные ответы.
    NO_SELLABLE_MASTER = "NO_SELLABLE_MASTER"
    #: Мастера есть, но ни один не прошёл; либо не прошло само предложение.
    NOT_ADMITTED = "NOT_ADMITTED"


@dataclass(frozen=True)
class OfferAdmission:
    """Допуск одного предложения салона."""

    verdict: OfferVerdict
    #: Восемь ответов уровня предложения (мастер ``None``).
    offer: tuple[CheckAnswer, ...]
    #: Мастер → восемь ответов этой пары; только продаваемые мастера.
    masters: dict[UUID, tuple[CheckAnswer, ...]]
    #: При ``NOT_ADMITTED`` — что не сошлось: ответ уровня предложения, а если
    #: предложение чисто — по одному первому несошедшемуся от каждого мастера
    #: (без повторов). Ответ отдаётся целиком: у «не действует» кода причины
    #: нет, и подменять его кодом отказа нельзя.
    unmet: tuple[CheckAnswer, ...] = ()
    #: Предложение — помеченные синтетические данные, прочитанные под
    #: действующим разрешением спрашивающего (DRF-2916). При любом вердикте.
    #: У настоящего предложения всегда ``False``; синтетическое без разрешения
    #: в ответ не попадает вовсе.
    synthetic: bool = False


def sellable_edges(
    salon_service_ids, *, viewer=None, all_salons: bool = False, _as_synthetic: frozenset = frozenset(),
) -> dict[UUID, list[UUID]]:
    """Продаваемые мастера предложений — тем же правилом, что у пула подбора.

    Мастер продаётся (:func:`users.sellable.sellable_q`), его салон жив, ребро
    «мастер × услуга» продаётся (:func:`services.offer_sellable.sellable_offer_q`).
    Демо-салоны: обычному клиенту не показываются, тестовой личности
    показываются — по ``viewer``, как в подборе. ``all_salons=True`` —
    операторский режим: демо наравне, правило видимости не применяется.
    Назвать и личность, и операторский режим сразу нельзя.
    """
    if all_salons and viewer is not None:
        raise ValueError("либо личность спрашивающего, либо операторский режим «все салоны» — не оба")
    visible = demo_scope_q(True) if all_salons else demo_visibility_q(viewer)
    masters = SpecialistProfile.objects.filter(sellable_q(), visible, tenant__is_active=True)
    edges: dict[UUID, list[UUID]] = defaultdict(list)
    for salon_id, specialist_id in (
        SpecialistService.objects
        .filter(
            sellable_offer_q(),
            # Синтетическое предложение — только под разрешением и только у
            # мастера его же салона (второй рубеж к замку базы).
            real_offer_q("salon_service__")
            | Q(salon_service_id__in=_as_synthetic, specialist__tenant_id=F("salon_service__tenant_id")),
            salon_service_id__in=list(salon_service_ids), specialist__in=masters,
        )
        .order_by("salon_service_id", "specialist_id")
        .values_list("salon_service_id", "specialist_id")
    ):
        edges[salon_id].append(specialist_id)
    return edges


def offer_admission(
    salon_service_ids: Iterable[UUID],
    *,
    viewer=None,
    all_salons: bool = False,
    enabled: Iterable[AdmissionCheck] = ALL_CHECKS,
    not_enforced_is_unmet: bool = False,
) -> dict[UUID, OfferAdmission]:
    """Вердикт по каждому предложению: открыто, некому оказать, не допущено.

    ``enabled`` и ``not_enforced_is_unmet`` — как у
    :func:`recommendation.api.first_unmet`: набор проверок меньше полного —
    только для диагностики; строгая свёртка считает «не действует»
    несошедшейся, не превращая её в отказ.

    Предложение, которого нет в базе, в ответ не попадает.
    """
    ids = list(dict.fromkeys(salon_service_ids))
    if not ids:
        return {}
    # Разрешение на синтетику выводится из личности и только из неё: у
    # операторского режима и у неизвестного клиента его нет.
    as_synthetic = offers_reading_as_synthetic(ids, include_synthetic=None if all_salons else grant_for(viewer))
    templates = dict(
        SalonService.objects.filter(real_offer_q() | Q(pk__in=as_synthetic), pk__in=ids)
        .values_list("pk", "template_id")
    )
    edges = sellable_edges(templates, viewer=viewer, all_salons=all_salons, _as_synthetic=as_synthetic)
    rows: list[Row] = []
    for salon_id, template_id in templates.items():
        rows.append((None, salon_id, template_id))
        rows.extend((specialist_id, salon_id, template_id) for specialist_id in edges.get(salon_id, ()))
    answers = admission_answers(rows, viewer=None if all_salons else viewer)
    enabled = frozenset(enabled)

    def unmet(found: tuple[CheckAnswer, ...]) -> CheckAnswer | None:
        return first_unmet(found, enabled=enabled, not_enforced_is_unmet=not_enforced_is_unmet)

    out: dict[UUID, OfferAdmission] = {}
    for salon_id in templates:
        offer = answers[(None, salon_id)]
        masters = {specialist_id: answers[(specialist_id, salon_id)] for specialist_id in edges.get(salon_id, ())}
        synthetic = salon_id in as_synthetic
        offer_unmet = unmet(offer)
        if offer_unmet is not None:
            out[salon_id] = OfferAdmission(OfferVerdict.NOT_ADMITTED, offer, masters, (offer_unmet,), synthetic)
            continue
        if not masters:
            out[salon_id] = OfferAdmission(OfferVerdict.NO_SELLABLE_MASTER, offer, masters, synthetic=synthetic)
            continue
        per_master = [unmet(found) for found in masters.values()]
        if any(found is None for found in per_master):
            out[salon_id] = OfferAdmission(OfferVerdict.OPEN, offer, masters, synthetic=synthetic)
            continue
        out[salon_id] = OfferAdmission(
            OfferVerdict.NOT_ADMITTED, offer, masters, tuple(dict.fromkeys(per_master)), synthetic,
        )
    return out


__all__ = [
    "OfferAdmission",
    "OfferVerdict",
    "admission_answers",
    "offer_admission",
    "sellable_edges",
]
