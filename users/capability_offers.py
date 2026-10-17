"""Предложения салонов по способности процедуры — вход для шага плана (DRF-2915).

Шаг плана несёт одну способность («что должно быть сделано»), а записаться
можно только на предложение салона. Эта функция отвечает на первый вопрос
пути «шаг → услуга»: какие предложения, видимые этому клиенту, стоят на
каноне с подтверждённой способностью.

Чего она НЕ отвечает — допущено ли предложение и есть ли кому его оказать:
это :func:`users.admission.offer_admission` поверх найденных id, теми же
восемью проверками, что и у подбора. Поиск допуск не обходит и не повторяет.

Пустой ответ называет причину — закрытым списком, а не логом: «способности
нет», «предложений нет» и «предложения не для этого клиента» — три разные
ситуации, и склеивать их в одно «не нашла» решает не каталог.

Только чтение. Видимость — та же, что у подбора: демо-салоны обычному
клиенту не показываются (:func:`users.sellable.demo_visibility_q`).
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from django.db.models import Q

from services.capabilities import template_ids_by_capability
from services.models import SalonService
from services.synthetic import grant_for, reads_synthetic, real_offer_q
from users.sellable import demo_visibility_q


class NoOffers(StrEnum):
    #: Ни у одного канона нет этой способности — подтверждённой и действующей.
    NO_CAPABILITY = "NO_CAPABILITY"
    #: Канон со способностью есть, но ни один живой салон его не предлагает.
    NO_OFFER = "NO_OFFER"
    #: Предложения есть, но все — вне видимости этого клиента. Внутренний
    #: ответ: человеку факт существования таких предложений не сообщается.
    OUT_OF_SIGHT = "OUT_OF_SIGHT"


@dataclass(frozen=True)
class CapabilityOffers:
    #: Предложения в видимости клиента, в устойчивом порядке (по id).
    offer_ids: tuple[UUID, ...]
    #: Почему пусто; ``None`` тогда и только тогда, когда предложения есть.
    empty_because: NoOffers | None = None


def offers_by_capability(key: str, *, viewer=None) -> CapabilityOffers:
    """Предложения салонов на канонах с подтверждённой способностью ``key``.

    ``viewer`` — пользователь каталога, для которого идёт подбор; ``None`` —
    неизвестный клиент (демо скрыто). Предложение считается существующим,
    если оно активно и его салон жив; продаваемость мастеров и допуск сюда
    не входят.
    """
    return offers_by_capabilities([key], viewer=viewer)[key]


def offers_by_capabilities(keys: Iterable[str], *, viewer=None) -> dict[str, CapabilityOffers]:
    """То же по нескольким способностям сразу — «что из перечня цели исполнимо» (DRF-2966).

    На КАЖДЫЙ запрошенный ключ — ровно тот ответ, что дал бы одиночный вызов:
    ключ, которого никто не знает, в ответе есть, с ``NO_CAPABILITY``.
    Повторы ключей схлопываются. Число запросов от числа ключей не зависит;
    пустой вход — пустой ответ без запросов.

    Допуск сюда не входит и здесь: вызывающий зовёт
    :func:`users.admission.offer_admission` один раз на все найденные id —
    строгость свёртки остаётся его решением.
    """
    wanted = list(dict.fromkeys(keys))
    if not wanted:
        return {}
    # Разрешение на помеченную синтетику — только из личности (DRF-2916).
    # Без него синтетическая способность — «нет такой способности»: её
    # существование не раскрывается.
    grant = grant_for(viewer)
    templates = template_ids_by_capability(wanted, include_synthetic=grant)
    every_template = frozenset().union(*templates.values())
    by_template: dict[UUID, list[tuple[UUID, bool]]] = defaultdict(list)
    if every_template:
        # Синтетическая услуга на настоящем каноне невозможна по замку базы
        # (DRF-2916); условие здесь — второй рубеж, а не единственный.
        genuine = real_offer_q() if not reads_synthetic(grant) else Q()
        existing = SalonService.objects.filter(
            genuine, template_id__in=every_template, is_active=True, tenant__is_active=True,
        )
        visible = frozenset(existing.filter(demo_visibility_q(viewer)).values_list("pk", flat=True))
        for offer_id, template_id in existing.values_list("pk", "template_id"):
            by_template[template_id].append((offer_id, offer_id in visible))

    out: dict[str, CapabilityOffers] = {}
    for key in wanted:
        if not templates[key]:
            out[key] = CapabilityOffers((), NoOffers.NO_CAPABILITY)
            continue
        offers = [row for template_id in templates[key] for row in by_template.get(template_id, ())]
        in_sight = tuple(sorted(offer_id for offer_id, seen in offers if seen))
        if in_sight:
            out[key] = CapabilityOffers(in_sight)
        else:
            out[key] = CapabilityOffers((), NoOffers.OUT_OF_SIGHT if offers else NoOffers.NO_OFFER)
    return out


__all__ = ["CapabilityOffers", "NoOffers", "offers_by_capabilities", "offers_by_capability"]
