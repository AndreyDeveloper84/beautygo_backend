"""Продаётся ли предложение — одно определение на весь путь продажи (DRF-1962).

Решение владельца 15.09 (§10): «0 ₽ не является реальной продажной ценой».
Предложение с ценой ниже 1 ₽ не показывается в продаваемом каталоге, не даёт
слотов, не бронируется и не исполняет рекомендацию, пока реальная цена не
станет ≥ 1. Цена не выдумывается, валидатор формы не ослабляется, маппинг
услуги не трогается.

Цена правила — цена ребра ``SpecialistService.price``: авторитетна опция,
которую видел клиент, и её же снимает запись (D4). ``SalonService.base_price``
в правиле не участвует. Легаси ``Service`` идёт через то же правило со своей
ценой.

Почему модуль, а не проверка в каждом месте: до правки на пути продажи было
семь независимых написаний «активное предложение», и ни одно не смотрело на
цену. Перепись ``services/tests/test_offer_price_sellable_1962.py`` краснеет
на сыром фильтре продажи вне этого модуля.

Мастер (продаётся ли человек) — отдельный вопрос, ``users.sellable``.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Q

#: Наименьшая реальная продажная цена, ₽.
OFFER_MIN_PRICE = Decimal("1")

#: Имена причин — наружу (``details.reason`` отказа записи, зеркало бота).
PRICE_BELOW_MINIMUM = "price_below_minimum"
OFFER_INACTIVE = "inactive"


def sellable_offer_q(prefix: str = "") -> Q:
    """Ребро ``SpecialistService`` продаётся: оно активно, салонная услуга
    активна, цена ≥ 1.

    ``prefix`` — путь от запрашиваемой модели до ребра, например
    ``"specialist_services__"``. Условия стоят в ОДНОМ ``Q``, чтобы через
    многозначную связь они относились к одному и тому же ребру.
    """
    return Q(**{
        f"{prefix}is_active": True,
        f"{prefix}salon_service__is_active": True,
        f"{prefix}price__gte": OFFER_MIN_PRICE,
    })


def sellable_legacy_q(prefix: str = "") -> Q:
    """Легаси ``Service`` продаётся: он активен и цена ≥ 1."""
    return Q(**{
        f"{prefix}is_active": True,
        f"{prefix}price__gte": OFFER_MIN_PRICE,
    })


def offer_refusal(price) -> str | None:
    """Причина, по которой цена не продаёт предложение, или ``None``."""
    if price is None or Decimal(price) < OFFER_MIN_PRICE:
        return PRICE_BELOW_MINIMUM
    return None


def edge_refusal(link) -> str | None:
    """Причина, по которой ребро не продаётся, или ``None``.

    Активность раньше цены: неактивное ребро не продаётся независимо от цены,
    и называть его ценой было бы неправдой о причине.
    """
    if not link.is_active or not link.salon_service.is_active:
        return OFFER_INACTIVE
    return offer_refusal(link.price)
