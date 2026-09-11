"""Порядок карточек сохраняется только с названным происхождением (В-16).

Почему прежней защиты было мало
-------------------------------

`handle_show_specialists` уже не брал порядок из ответа модели — и это
правильно: порядок есть политика ранжирования, `LLM_FORBIDDEN`. Но
восстанавливал он порядок **легаси-движка**, то есть защита от неверного
авторитета стояла поверх другого неверного авторитета.

Решение владельца В-16 дословно::

    «LLM не переставляла кандидатов» — недостаточно.
    Потребитель вправе сохранять смысловой порядок только когда
    происхождение порядка называет канонический Recommendation Authority.
    Порядок легаси-движка, сохранённый идеально, остаётся порядком
    легаси-движка.

Почему сторож здесь поведенческий, а не синтаксический
------------------------------------------------------

Прежний `test_no_ranking_outside_the_resolver` ищет вызовы `sorted` и
`order_by` с признаком качества. Потребитель, **наследующий** чужой
порядок, ему невидим по устройству: он ничего не сортирует по рейтингу,
он воспроизводит позицию. Именно так этот дефект и прожил.

Поэтому ниже проверяется не текст, а **поведение**: подаётся вход в
заведомом порядке и смотрится, воспроизвёлся ли он. Такой сторож не
обойти переименованием и не удовлетворить комментарием.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from ai.application.services.specialist_context_builder import (
    OrderProvenance,
    SpecialistCandidate,
    SpecialistContext,
)
from ai.tools_handlers import handle_show_specialists

#: Имена нарочно в порядке, обратном алфавитному: если бы обработчик
#: просто сохранял вход, нейтральный порядок совпал бы с ним случайно.
NAMES = ["Яна", "Борис", "Анна"]


def _candidates() -> list[SpecialistCandidate]:
    return [
        SpecialistCandidate(
            id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
            display_name=name,
            rating=Decimal("4.8"),
            reviews_count=10,
            address="",
            distance_km=None,
            services_preview=[],
        )
        for name in NAMES
    ]


#: В каком порядке кандидатов перечисляет модель. Выбран ТРЕТЬЕЙ
#: перестановкой — не порядком контекста и не алфавитным.
#:
#: Первая версия просила обратный порядок, и он случайно совпал с
#: алфавитным: тест «модель не влияет» стал неотличим от теста
#: «порядок нейтральный» и покраснел на верном коде. Фикстура, в
#: которой два разных ответа выглядят одинаково, не проверяет ничего.
ASKED_BY_MODEL = ["Борис", "Анна", "Яна"]


def _shown(
    context: SpecialistContext, asked_names: list[str] | None = None,
) -> list[str]:
    by_name = {c.display_name: c for c in context.candidates}
    asked = asked_names or ASKED_BY_MODEL
    result = handle_show_specialists(
        {
            "specialist_ids": [str(by_name[name].id) for name in asked],
            "explanation": "test",
        },
        context,
    )
    by_id = {str(c.id): c.display_name for c in context.candidates}
    return [by_id[s["specialist"]["id"]] for s in result.action_data["specialists"]]


# ---------------------------------------------------------------------------
# Порядок движка не наследуется
# ---------------------------------------------------------------------------


def test_legacy_engine_order_is_not_reproduced():
    """Порядок движка карточками не воспроизводится.

    Это и есть В-16: сохранённость ничего не доказывает, пока не
    названо, чей это порядок. Здесь названо — движок, — и потому
    сохранять нечего.
    """
    candidates = _candidates()
    context = SpecialistContext(
        order_provenance=OrderProvenance.LEGACY_ENGINE, candidates=candidates,
    )
    assert _shown(context) != NAMES


def test_without_authority_the_order_is_neutral():
    """Вместо чужого порядка — нейтральный, по имени.

    Та же форма, что у промпта (DRF-1630), и по той же причине: алфавит
    человек читает как список, а не как рейтинг.
    """
    candidates = _candidates()
    context = SpecialistContext(
        order_provenance=OrderProvenance.LEGACY_ENGINE, candidates=candidates,
    )
    assert _shown(context) == ["Анна", "Борис", "Яна"]


def test_neutral_provenance_is_also_not_reproduced_as_meaningful():
    """`NEUTRAL` ведёт себя как `LEGACY_ENGINE`: смысла в порядке нет.

    Проверка не лишняя: соблазн написать `!= CANONICAL_RESOLVER` в одном
    месте и `is LEGACY_ENGINE` в другом велик, и тогда третье значение
    поведёт себя как авторитетное, никем этого не заметив.
    """
    candidates = _candidates()
    context = SpecialistContext(
        order_provenance=OrderProvenance.NEUTRAL, candidates=candidates,
    )
    assert _shown(context) == ["Анна", "Борис", "Яна"]


# ---------------------------------------------------------------------------
# Положительная стража: авторитетный порядок сохраняется
# ---------------------------------------------------------------------------


def test_canonical_order_is_preserved_exactly():
    """Порядок, названный авторитетом, сохраняется как есть.

    Без этой проверки три выше зеленели бы и на обработчике, который
    всегда сортирует по алфавиту, — то есть граница была бы «проведена»
    удалением самой возможности её соблюсти. Переставить авторитетный
    порядок — нарушение с другой стороны.
    """
    candidates = _candidates()
    context = SpecialistContext(
        order_provenance=OrderProvenance.CANONICAL_RESOLVER, candidates=candidates,
    )
    assert _shown(context) == NAMES


def test_the_model_order_never_wins():
    """Ни при каком происхождении порядок не берётся у модели.

    Прежнее свойство не потеряно — оно было верным и остаётся.
    Проверяется по ВСЕМ значениям происхождения, а не по одному:
    правку легко «упростить» до «сортируем как попросили», и тогда
    зелёными останутся ровно те случаи, где порядок и так совпадает.
    """
    candidates = _candidates()
    for provenance in OrderProvenance:
        context = SpecialistContext(
            order_provenance=provenance, candidates=candidates,
        )
        assert _shown(context) != ASKED_BY_MODEL, provenance


# ---------------------------------------------------------------------------
# Промолчать о происхождении нельзя
# ---------------------------------------------------------------------------


def test_context_cannot_be_built_without_naming_the_provenance():
    """Поле обязательно, и это единственное, что заставляет назвать источник.

    Умолчание здесь было бы худшим из решений: следующий производитель
    контекста промолчал бы ровно так же, как молчал прежний, и дефект
    вернулся бы тем же путём — не через злой умысел, а через незамеченное
    поле.
    """
    with pytest.raises(TypeError):
        SpecialistContext(candidates=_candidates())
