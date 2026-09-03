"""Анкета цели — серверная последовательность вопросов (DRF-1451).

Решение владельца 03.09.2026, поправка A-1 к BOT-001 (§24): клиент,
впервые открывший мини-приложение, попадает на анкету; по её завершении
формируется цель.

Где живут вопросы и почему именно здесь
---------------------------------------

Здесь и только здесь. Условие C-1 поправки: «Каждый вопрос и решение о
том, какой вопрос следующий, принадлежат серверной логике и приходят в
мини-приложение данными». Экран получает ОДИН текущий шаг внутри
``missing`` и не знает ни сколько шагов всего останется, ни какой
придёт следующим — он даже порядок не вычисляет, потому что порядок
приходит готовым номером в ``progress``.

Это не послабление non-goal #5 BOT-001 («No independent Mini App
conversational implementation»), а его исполнение: анкета — проекция
серверного механизма ``missing``, а не клиентский мастер.

Устройство прохода
------------------

Сужающие шаги (``ANKETA_STEPS``) — с закрытым списком вариантов; ответы
на них durable, это будущий корпус формулировок (OD-2).

Финальный шаг (``FINAL_STEP_KEY``) — сама цель: варианты берутся из
курируемых ``GoalOption``, свободный ввод разрешён. Ответ на него и
создаёт ``ClientGoal``; отдельной «кнопки завершить» нет, потому что
завершение анкеты и есть выбор цели.

Чего здесь НЕТ
--------------

Ворот. Пока проход открыт, документ по-прежнему несёт ``suggestions`` и
намерение ``formulate_own``: назвать услугу и уйти к подбору можно на
любом шаге, не ответив ни на один вопрос (C-2). За это отвечает
``decision_context`` и ``api``, здесь — только описание вопросов.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.models import GoalOption

MISSING_GOAL_ANKETA = "goal_anketa"

FINAL_STEP_KEY = "goal"


@dataclass(frozen=True)
class AnketaStep:
    """Один вопрос анкеты — ровно то, что уедет в ``missing``."""

    key: str
    prompt: str
    options: tuple[tuple[str, str], ...] = ()
    allow_free_text: bool = False


# Сужающие шаги. Держатся короткими сознательно: анкета — вход, а не
# профилирование. Условие C-3 поправки — «собирается только цель»:
# ни контактов, ни персональных данных сверх §13.1 BOT-001 здесь нет.
ANKETA_STEPS: tuple[AnketaStep, ...] = (
    AnketaStep(
        key="area",
        prompt="Что сейчас хочется привести в порядок?",
        options=(
            ("face", "Лицо и кожа"),
            ("body", "Тело и вес"),
            ("hair", "Волосы"),
            ("hands", "Руки и ногти"),
            ("overall", "Общее состояние"),
        ),
    ),
    AnketaStep(
        key="feeling",
        prompt="Как хочешь себя чувствовать после?",
        options=(
            ("rested", "Отдохнувшей"),
            ("confident", "Увереннее"),
            ("groomed", "Ухоженной"),
            ("lighter", "Легче и бодрее"),
            ("calmer", "Спокойнее"),
        ),
    ),
)

FINAL_STEP_PROMPT = "Выбери цель — или напиши своими словами, чего хочешь."

# Полное число шагов прохода: сужающие + финальный.
TOTAL_STEPS = len(ANKETA_STEPS) + 1

_STEP_BY_KEY = {step.key: step for step in ANKETA_STEPS}

_ANSWERABLE_KEYS = frozenset({*_STEP_BY_KEY, FINAL_STEP_KEY})


def is_answerable_step(step_key: str) -> bool:
    """Известен ли серверу такой шаг вообще."""
    return step_key in _ANSWERABLE_KEYS


def _final_step() -> AnketaStep:
    """Финальный шаг: варианты — курируемые цели, свободный ввод открыт."""
    options = tuple(
        (option.key, option.label)
        for option in GoalOption.objects.filter(is_active=True)
    )
    return AnketaStep(
        key=FINAL_STEP_KEY,
        prompt=FINAL_STEP_PROMPT,
        options=options,
        allow_free_text=True,
    )


def next_step(answered_keys: set[str]) -> AnketaStep:
    """Какой шаг задавать при уже отвеченных ``answered_keys``.

    Порядок — единственный источник правды о последовательности, и он
    целиком здесь. Финальный шаг возвращается, когда сужающие
    закончились: анкета всегда завершается выбором цели.
    """
    for step in ANKETA_STEPS:
        if step.key not in answered_keys:
            return step
    return _final_step()


def step_index(step_key: str) -> int:
    """Человеческий номер шага, 1-based. Финальный — последний."""
    for index, step in enumerate(ANKETA_STEPS, start=1):
        if step.key == step_key:
            return index
    return TOTAL_STEPS


def as_missing_item(step: AnketaStep) -> dict[str, Any]:
    """Шаг → элемент ``missing``, готовый к отрисовке как есть.

    Форма расширяет DRF-1190, а не ломает его: ``kind`` и ``prompt`` на
    прежних местах, поэтому потребитель, читающий только их
    (``GoalInviteCard``), продолжает работать без правки.
    """
    return {
        "kind": MISSING_GOAL_ANKETA,
        "prompt": step.prompt,
        "step": step.key,
        "options": [{"key": key, "label": label} for key, label in step.options],
        "allow_free_text": step.allow_free_text,
        "progress": {"index": step_index(step.key), "total": TOTAL_STEPS},
    }
