"""Замер покрытия целей мастерами — та же выдача, что видит клиент.

Зачем отдельный модуль
----------------------
Владелец меряет каталог вопросом «сколько мастеров найдётся, если клиент
выберет эту цель». Ответ на него даёт ровно один компонент —
``RecommendationEngine``, — и любая попытка пересчитать то же самое
своим запросом создаёт вторую, расходящуюся правду: движок фильтрует по
``status``/``is_available``/``is_booking_enabled``/``rating``, раскрывает
цель вниз по дереву категорий и умеет фолбэк на категорию шаблона.
Повторить это в сиде значило бы доказывать теоремой о своём запросе
свойство чужого.

Поэтому здесь нет собственного SQL: цель раскрывается штатным
``expand_categories_with_descendants`` и подаётся в штатный движок.
Замер и продакшен-выдача не могут разойтись по построению.

``use_cache=False`` обязателен: движок кеширует результат на
``AI_REC_CACHE_TTL``, и замер «до/после» на одном процессе прочитал бы
дважды один и тот же ответ.
"""
from __future__ import annotations

from .goal_resolution import expand_categories_with_descendants
from .models import GoalOption, GoalOptionCategory

# Выше любого мыслимого числа мастеров на пилоте. Движок режет выдачу до
# ``limit``, а замер покрытия обязан считать всех, кого цель находит, —
# иначе «6 мастеров» означало бы «не меньше шести», и рост покрытия был
# бы неотличим от упора в потолок.
COVERAGE_LIMIT = 1000


def goal_master_coverage(*, limit: int = COVERAGE_LIMIT) -> dict[str, int]:
    """``{goal_key: сколько мастеров вернёт подбор по этой цели}``.

    Порядок ключей — ``sort_order`` цели, как на экране подсказок.
    Цель без связей даёт 0 и не ходит в движок: пустой
    ``goal_category_ids`` движок трактует как «фильтра нет» и вернул бы
    всех мастеров разом — то есть незакураированная цель выглядела бы
    как полностью покрытая.
    """
    # Локальный импорт: ``ai.application`` тянет за собой слой LLM, а
    # модуль читается и из management-команды, и из тестов каталога.
    from ai.application.services.recommendation_engine import (
        RecommendationEngine,
        RecommendationQuery,
    )

    engine = RecommendationEngine()
    coverage: dict[str, int] = {}
    for option in GoalOption.objects.filter(is_active=True).order_by(
        "sort_order", "key",
    ):
        bound = list(
            GoalOptionCategory.objects.filter(goal_option=option)
            .order_by("sort_order")
            .values_list("category_id", flat=True)
        )
        category_ids = expand_categories_with_descendants(bound)
        if not category_ids:
            coverage[option.key] = 0
            continue
        result = engine.recommend(
            RecommendationQuery(
                goal_category_ids=tuple(category_ids), limit=limit,
            ),
            use_cache=False,
        )
        coverage[option.key] = len(result.candidates)
    return coverage
