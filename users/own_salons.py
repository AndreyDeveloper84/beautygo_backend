"""«Свои салоны» клиента — один предикат на все поверхности (O-1b, DRF-2831).

Салон свой, когда у клиента с ним действующее отношение покупателя
(``TenantUserRelationship``, роль ``customer``). Читают его полка «Твои
места» и режим ``OWN_SALONS`` резолвера; правило у них одно, поэтому и
место одно — две копии разошлись бы.
"""
from __future__ import annotations

from uuid import UUID

from users.models import TenantUserRelationship


def own_salon_ids(user) -> tuple[UUID, ...]:
    """Салоны, с которыми у клиента действующее отношение покупателя."""
    return tuple(
        TenantUserRelationship.objects
        .filter(
            user=user,
            is_active=True,
            role=TenantUserRelationship.Role.CUSTOMER,
        )
        .order_by("tenant_id")
        .values_list("tenant_id", flat=True)
    )


def _fold_name(text: str) -> str:
    """Без регистра и без различия «ё»/«е»: «Алёна» в памяти и «Алена» в каталоге — одно имя."""
    return text.strip().casefold().replace("ё", "е")


def masters_matching_name(stems, tenant_ids, *, viewer=None) -> tuple[UUID, ...]:
    """Мастера салонов ``tenant_ids``, чьё имя отвечает ВСЕМ основам — DRF-2855.

    Основа отвечает, когда входит в слово ``display_name`` (без учёта
    регистра и «ё»/«е»): так же ищет мастера по имени бот, и клиент, спросивший «к
    Анне», получает тех же людей. Морфологии здесь нет — основы приходят
    готовыми.

    Пул — продаваемые мастера живых салонов, видимые этому клиенту: те же
    условия, что у пула подбора, и ДО его гейтов. Мастер, закрытый гейтом,
    считается: «Анна есть, но сейчас не рекомендуется» и «Анны нет» — разные
    ответы, а вторая Анна остаётся второй. Возвращаются ключи пользователей
    — ими мастера называет подбор.
    """
    from users.models import SpecialistProfile
    from users.sellable import demo_visibility_q, sellable_q

    wanted = [folded for folded in map(_fold_name, stems) if folded]
    if not wanted or not tenant_ids:
        return ()
    rows = (
        SpecialistProfile.objects
        .filter(sellable_q(), demo_visibility_q(viewer), tenant__is_active=True, tenant_id__in=tenant_ids)
        .order_by("user_id")
        .values_list("user_id", "display_name")
    )
    return tuple(
        user_id for user_id, display_name in rows
        if all(any(stem in word for word in _fold_name(display_name or "").split()) for stem in wanted)
    )
