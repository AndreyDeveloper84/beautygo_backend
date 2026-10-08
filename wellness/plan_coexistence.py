"""Plan Engine и Plan Lite: когда новый механизм вытесняет прежний (DRF-2857).

Решение WP1: при включённом Plan Engine у человека один механизм плана —
писатель Lite отказывает, а сохранение durable-плана закрывает действующий
Lite-план как «замещён».

Решение владельца 08.10 (сквозная проверка плана на подготовленных тестовых
данных): существующее создание через Lite не ломается, пока новый путь не
готов. Проверка идёт на стенде с включённым движком и включёнными тестовыми
данными — и на это время оба механизма сосуществуют. Вытеснение не снято:
оно подавлено, пока включён ``SYNTHETIC_TEST_DATA_ENABLED``; выключили —
действует как решено в WP1.

Оба места вытеснения читают одну эту функцию, чтобы не разойтись.
"""
from __future__ import annotations

from django.conf import settings


def engine_displaces_lite() -> bool:
    """Вытесняет ли сейчас Plan Engine механизм Plan Lite."""
    if not getattr(settings, "PLAN_ENGINE_ENABLED", False):
        return False
    # Настройку объявляет каталог знания (services); здесь она только читается.
    return not getattr(settings, "SYNTHETIC_TEST_DATA_ENABLED", False)
