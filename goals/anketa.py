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
придёт следующим — он даже «это последний» не вычисляет, потому что
признак приходит готовым в ``progress.is_last``.

Числа в ``progress`` (DRF-1743, доктрина 12.09). ``index``/``total``
экран больше не рисует: «Вопрос 2 из 3» — счётчик, который честен
только пока порядок вопросов фиксирован; как только вопросы задаёт
движок (DRF-1533), общее число неизвестно заранее, и число стало бы
выдумкой. Макет C03 (DRF-1178): «Ещё один короткий вопрос» — только
если действительно уверены, что вопрос последний. Поэтому наружу едет
факт, который сервер гарантирует, — ``is_last`` — а ``index``/``total``
остаются на один релиз ради старой сборки экрана и снимаются следующим
PR.

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
    """Один вопрос анкеты — ровно то, что уедет в ``missing``.

    ``rule_ids`` — правила, через которые ответ на этот шаг влияет на
    решение (§5.3 решений владельца 11.09.2026). Пустой кортеж — честное
    «пока ни на что»: тогда человеку это говорится прямо в тексте шага
    (:func:`shown_prompt`), а не подразумевается вопросом. Непустой —
    каждый id обязан иметь читателя в :data:`RULE_READERS`; за парой
    следит :func:`influence_declaration_errors`.
    """

    key: str
    prompt: str
    options: tuple[tuple[str, str], ...] = ()
    allow_free_text: bool = False
    rule_ids: tuple[str, ...] = ()


# ─── влияние ответа на решение (§5.3) ─────────────────────────────────────
#
# «Если система не может показать rule_id, где поле повлияло на решение,
# нельзя заявлять, что поле было учтено». Замер 11.09
# (docs/MEASUREMENT_AREA_FEELING_5_3.md): ответы на ``area`` и ``feeling``
# пишутся и не читаются — 0 читателей значения, 0 влияния на выдачу; при
# этом сами вопросы с прогрессом «шаг 1 из 3» читаются человеком как
# учтённые. Пока правил нет, это говорится ему словами.

#: Пометка на шаге, ответ на который ни на что не влияет. Одна строка на
#: все такие шаги: разные формулировки одного и того же факта дали бы
#: разное впечатление о разных полях.
NO_INFLUENCE_NOTE = "Ответ сохраню, на подбор он пока не влияет."

#: rule_id → путь функции, которая этот ответ читает и применяет к
#: решению. Заводить rule_id без читателя нельзя (сторож ниже) — иначе
#: обещание «учтено» снова окажется без адреса.
RULE_READERS: dict[str, str] = {
    # Финальный шаг: выбранная цель → категории → фильтр выдачи.
    "goal.category_match": "goals.resolution.resolve_goal_category_ids",
}


def shown_prompt(step: AnketaStep) -> str:
    """Текст шага, каким его увидит человек.

    Без правил — вопрос плюс пометка; с правилами — вопрос как есть.
    Пометка приходит с сервера в самом ``prompt`` (условие C-1: экран
    рисует, не решает), поэтому мини-приложению для неё правка не нужна.
    """
    if step.rule_ids:
        return step.prompt
    return f"{step.prompt} {NO_INFLUENCE_NOTE}"


def influence_declaration_errors(steps: tuple[AnketaStep, ...]) -> list[str]:
    """Сторож класса DRF-1656: у каждого шага либо читатель, либо пометка.

    Возвращает список нарушений (пусто — чисто), чтобы тест печатал
    ВСЕ, а не первое. Проверяет обе половины, потому что они ломаются
    в разные стороны: rule_id без читателя — «учтено» без адреса,
    читатель без rule_id — влияние без объявления.
    """
    import importlib

    errors: list[str] = []
    for step in steps:
        prompt = shown_prompt(step)
        if not step.rule_ids:
            if NO_INFLUENCE_NOTE not in prompt:
                errors.append(f"{step.key}: нет правил и нет пометки")
            continue
        if NO_INFLUENCE_NOTE in prompt:
            errors.append(f"{step.key}: есть правила, но текст говорит «не влияет»")
        for rule_id in step.rule_ids:
            path = RULE_READERS.get(rule_id)
            if not path:
                errors.append(f"{step.key}: rule_id {rule_id!r} без читателя в RULE_READERS")
                continue
            module_name, _, attr = path.rpartition(".")
            try:
                reader = getattr(importlib.import_module(module_name), attr)
            except (ImportError, AttributeError) as exc:
                errors.append(f"{step.key}: читатель {path!r} не импортируется: {exc}")
                continue
            if not callable(reader):
                errors.append(f"{step.key}: читатель {path!r} не вызываем")
    return errors


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
        # Единственный шаг, ответ на который доезжает до выдачи:
        # goal_key → GoalOptionCategory → фильтр главной и полок.
        rule_ids=("goal.category_match",),
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


def is_last_step(step: AnketaStep) -> bool:
    """Гарантирует ли сервер, что после этого шага вопросов не будет.

    Истина ровно для финального шага: ответ на него создаёт цель и
    закрывает проход (``api._answer_anketa``), а ``next_step`` после
    любого сужающего шага всегда находит следующий. Не «index == total»:
    равенство чисел — совпадение, а не гарантия.
    """
    return step.key == FINAL_STEP_KEY


def as_missing_item(step: AnketaStep) -> dict[str, Any]:
    """Шаг → элемент ``missing``, готовый к отрисовке как есть.

    Форма расширяет DRF-1190, а не ломает его: ``kind`` и ``prompt`` на
    прежних местах, поэтому потребитель, читающий только их
    (``GoalInviteCard``), продолжает работать без правки.
    """
    return {
        "kind": MISSING_GOAL_ANKETA,
        "prompt": shown_prompt(step),
        "step": step.key,
        "options": [{"key": key, "label": label} for key, label in step.options],
        "allow_free_text": step.allow_free_text,
        "progress": {
            "index": step_index(step.key),
            "total": TOTAL_STEPS,
            "is_last": is_last_step(step),
        },
    }
