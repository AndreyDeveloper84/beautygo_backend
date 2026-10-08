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

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from services.capabilities import template_ids_with_capability
from services.models import SalonService
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
    templates = template_ids_with_capability(key)
    if not templates:
        return CapabilityOffers((), NoOffers.NO_CAPABILITY)
    existing = SalonService.objects.filter(template_id__in=templates, is_active=True, tenant__is_active=True)
    visible = tuple(existing.filter(demo_visibility_q(viewer)).order_by("pk").values_list("pk", flat=True))
    if visible:
        return CapabilityOffers(visible)
    if existing.exists():
        return CapabilityOffers((), NoOffers.OUT_OF_SIGHT)
    return CapabilityOffers((), NoOffers.NO_OFFER)


__all__ = ["CapabilityOffers", "NoOffers", "offers_by_capability"]
