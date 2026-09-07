"""Привязка порта к доменной правде. Настройкой, а не импортом.

Резолвер не ходит в ORM — он принимает :class:`CandidateSource` (контракт §5).
Кто именно его реализует, решает контур: путь к фабрике лежит в
``settings.RECOMMENDATION_CANDIDATE_SOURCE``.

**Почему не импортировать доменный адаптер прямо здесь.** Тогда `recommendation`
знал бы про `users.models`, и «модуль за границей» превратился бы в модуль,
сросшийся с доменом по импортам: вынести его в `ayla-ai-core` после пилота
можно было бы только переписав стадии. Настройка стоит один вызов и
сохраняет направление зависимости: домен знает про резолвер, резолвер про
домен — нет.

**Пока адаптера нет, ручка честно недоступна.** Не пустая выдача: пустая
выдача — это утверждение «подходящих нет», а мы не искали. Ненастроенный
источник — состояние `UNAVAILABLE`, и оно обязано отличаться от
«посмотрели и не нашли» ровно так же, как недоступность отличается от
нарушения контракта (§9.4). Адаптер приезжает с T6 (DRF-1567).
"""
from __future__ import annotations

from django.conf import settings
from django.utils.module_loading import import_string

from ._types import CandidateSource


class CandidateSourceNotConfigured(RuntimeError):
    """``RECOMMENDATION_CANDIDATE_SOURCE`` не задан или не импортируется."""


def get_candidate_source() -> CandidateSource:
    """Вернуть источник фактов или сказать, что его нет.

    Значение настройки — путь к **фабрике** (вызываемому объекту без
    аргументов), а не к готовому объекту: источник может держать запросы
    и кеш на время одного вызова, и переиспользовать его между запросами
    значило бы кешировать доменную правду дольше, чем она верна.
    """
    path = getattr(settings, "RECOMMENDATION_CANDIDATE_SOURCE", None)
    if not path:
        raise CandidateSourceNotConfigured(
            "RECOMMENDATION_CANDIDATE_SOURCE не задан: доменный адаптер ещё не привязан (T6/DRF-1567). "
            "Это недоступность источника, а не пустая выдача"
        )
    try:
        factory = import_string(path)
    except ImportError as exc:
        raise CandidateSourceNotConfigured(f"RECOMMENDATION_CANDIDATE_SOURCE={path!r} не импортируется: {exc}") from exc
    return factory()
