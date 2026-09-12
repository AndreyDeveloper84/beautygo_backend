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

Устройство прохода (DRF-1764, решение владельца OD-C02-ORDER 12.09)
--------------------------------------------------------------------

Цель — ПЕРВЫЙ шаг (``GOAL_STEP_KEY``): варианты берутся из курируемых
``GoalOption``, свободный ввод разрешён. Ответ на него создаёт
``ClientGoal`` сразу — «Твоя цель» на следующих кадрах уже есть, и
сужающие вопросы задаются ПОД неё (макет C02: «Что именно хочется
изменить?» для «Лучше выглядеть»). До DRF-1764 цель была последним
шагом (DRF-1451: «завершение анкеты и есть выбор цели»); владелец
пересмотрел это (DRF-1349, 12.09 04:49, вариант A — цель первой).

Сужающие шаги (``ANKETA_STEPS``) — с закрытым списком вариантов; ответы
на них durable, это будущий корпус формулировок (OD-2). Формулировка
шага может зависеть от выбранной цели (``prompt_by_goal``); без
подходящей — общий ``prompt``.

Проход завершается ответом на последний сужающий шаг; отдельной
«кнопки завершить» нет. «Это последний вопрос» сервер вычисляет
(:func:`is_last_step`), а не выводит из номера.

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

GOAL_STEP_KEY = "goal"

# ─── типы ответа по смыслу (DRF-1746, макет C03 P11) ───────────────────────
#
# «C03 не должен выглядеть как серия одинаковых экранов с radio-кнопками».
# Режим — свойство шага и едет в ``missing`` данными (условие C-1): экран
# рисует компонент по ``mode`` и ничего не выводит. Отсутствие поля в
# документе = ``single`` — старый потребитель и старый документ совпадают.
# Какой вопрос каким режимом — содержание анкеты/каталога вопросов движка,
# не этот контракт; сегодняшние ``ANKETA_STEPS`` остаются ``single``.
MODE_SINGLE = "single"
MODE_MULTI = "multi"
#: Подтверждение известного — компонент DRF-1745; в контракте место
#: зарезервировано, чтобы экран знал имя заранее и рисовал его как
#: ``single`` до появления компонента.
MODE_CONFIRM = "confirm"
MODE_SCALE = "scale"
MODE_TEXT = "text"
ANSWER_MODES = frozenset({MODE_SINGLE, MODE_MULTI, MODE_CONFIRM, MODE_SCALE, MODE_TEXT})

#: Лимит короткого свободного ответа (режим ``text``). Едет в документе
#: как ``text_limit``, чтобы поле на экране и проверка на сервере были
#: одним числом.
TEXT_ANSWER_LIMIT = 120

# ─── «Не знаю» — полноценный ответ (DRF-1747, макет C03 P12) ────────────────
#
# «Не заставляем придумывать информацию ради прохождения. После „Не знаю“
# Ayla либо продолжает без факта, либо, только если действительно
# необходимо, задаёт более простой вопрос». Поэтому «Не знаю» — не пропуск
# и не отсутствие строки, а durable-ответ шага с ключом ``unknown``: шаг в
# этом проходе не задаётся повторно. Это UNKNOWN, не FLEXIBLE («не важно»):
# «не важно» снимает вопрос, «не знаю» оставляет факт неизвестным.
#
# На каких шагах стоит «Не знаю» — ``AnketaStep.escape``; по умолчанию там,
# где макет его показывает (вопросы о самочувствии/проявлениях), не на
# выборе цели. Окончательно решает дизайнер в ревью.
UNKNOWN_OPTION_KEY = "unknown"
UNKNOWN_OPTION_LABEL = "Не знаю"
#: Роль опции в контракте: экран рисует такую опцию тихо и отдельно от
#: вариантов. У обычных вариантов роли нет (аддитивно).
OPTION_ROLE_ESCAPE = "escape"
#: Происхождение факта — общий словарь с памятью сказанного бота
#: (согласовано с окном мозга 12.09): conversation / anketa / operator.
ORIGIN_ANKETA = "anketa"


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
    #: Формулировка под цель: ``GoalOption.key`` → вопрос (DRF-1764, макет
    #: C02 «формулировку адаптируем под выбранную цель»). Пары, а не dict,
    #: чтобы шаг оставался hashable/frozen. Нет пары — общий ``prompt``.
    prompt_by_goal: tuple[tuple[str, str], ...] = ()
    #: Тип ответа (DRF-1746): один из :data:`ANSWER_MODES`.
    mode: str = MODE_SINGLE
    #: Подписи концов шкалы (режим ``scale``): (низ, верх). Порядок
    #: делений — порядок ``options``.
    scale_ends: tuple[str, str] | None = None
    #: «Не знаю» на этом шаге (DRF-1747): опция с ролью ``escape``.
    escape: bool = False


def answerable_option_keys(step: AnketaStep) -> set[str]:
    """Ключи, которыми на шаг можно ответить: варианты плюс ``unknown``
    там, где шаг его предлагает. Единственный источник для проверки
    ответа — экран ключ не выдумывает, сервер его не угадывает."""
    keys = {key for key, _ in step.options}
    if step.escape:
        keys.add(UNKNOWN_OPTION_KEY)
    return keys


def step_contract_errors(steps: tuple[AnketaStep, ...]) -> list[str]:
    """Сторож формы шага под его режим (DRF-1746). Пусто — чисто.

    Режим обещает экрану компонент, и у компонента есть входы: ``text``
    без свободного ввода — поле, которое сервер отвергнет; ``scale`` без
    подписей концов — шкала без смысла делений; ``multi`` без вариантов
    — «Продолжить» над пустотой. Ловится здесь, а не на экране.
    """
    errors: list[str] = []
    for step in steps:
        if step.mode not in ANSWER_MODES:
            errors.append(f"{step.key}: неизвестный mode {step.mode!r}")
            continue
        if step.mode == MODE_TEXT and (step.options or not step.allow_free_text):
            errors.append(f"{step.key}: text — без вариантов и со свободным вводом")
        if step.mode == MODE_SCALE and (len(step.options) < 2 or not step.scale_ends):
            errors.append(f"{step.key}: scale — не меньше двух делений и подписи концов")
        if step.mode in (MODE_MULTI, MODE_SINGLE, MODE_CONFIRM) and step.scale_ends:
            errors.append(f"{step.key}: подписи концов только у scale")
        if step.mode == MODE_MULTI and not step.options:
            errors.append(f"{step.key}: multi — без вариантов нечего отмечать")
        if any(key == UNKNOWN_OPTION_KEY for key, _ in step.options):
            errors.append(f"{step.key}: {UNKNOWN_OPTION_KEY!r} — зарезервированный ключ escape")
        if step.key == GOAL_STEP_KEY and step.escape:
            errors.append(f"{step.key}: на выборе цели «Не знаю» не ставится")
    return errors


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


def prompt_for(step: AnketaStep, goal_key: str | None = None) -> str:
    """Голый вопрос шага под цель ``goal_key`` (DRF-1764); без пары — общий."""
    return dict(step.prompt_by_goal).get(goal_key or "", step.prompt)


def shown_prompt(step: AnketaStep, goal_key: str | None = None) -> str:
    """Текст шага, каким его увидит человек.

    Без правил — вопрос плюс пометка; с правилами — вопрос как есть.
    Пометка приходит с сервера в самом ``prompt`` (условие C-1: экран
    рисует, не решает), поэтому мини-приложению для неё правка не нужна.
    """
    prompt = prompt_for(step, goal_key)
    if step.rule_ids:
        return prompt
    return f"{prompt} {NO_INFLUENCE_NOTE}"


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
        # Все формулировки шага — общая и под каждую цель: пометка обязана
        # стоять в каждой, иначе «не влияет» говорилось бы не всем.
        prompts = [shown_prompt(step)] + [
            shown_prompt(step, goal_key) for goal_key, _ in step.prompt_by_goal
        ]
        if not step.rule_ids:
            for prompt in prompts:
                if NO_INFLUENCE_NOTE not in prompt:
                    errors.append(f"{step.key}: нет правил и нет пометки")
                    break
            continue
        if any(NO_INFLUENCE_NOTE in prompt for prompt in prompts):
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
        # Макет C02: «Что именно хочется изменить?» для целей про внешность,
        # «Что хочется наладить в первую очередь?» для «привести себя в
        # порядок». Ключи — из services/seeds/goal_options_2026-08.json;
        # свободная цель (goal_key NULL) получает общий вопрос.
        prompt_by_goal=(
            ("new_look", "Что именно хочется изменить?"),
            ("skin_care", "Что именно хочется изменить?"),
            ("body_shape", "Что именно хочется изменить?"),
            ("self_care", "Что хочется наладить в первую очередь?"),
            ("event", "Что важно привести в порядок к событию?"),
        ),
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
        # Макет C02: «Что сейчас хочется почувствовать по-другому?» для
        # «Расслабиться и восстановиться».
        prompt_by_goal=(
            ("relax", "Что сейчас хочется почувствовать по-другому?"),
            ("recharge", "Что сейчас хочется почувствовать по-другому?"),
        ),
        options=(
            ("rested", "Отдохнувшей"),
            ("confident", "Увереннее"),
            ("groomed", "Ухоженной"),
            ("lighter", "Легче и бодрее"),
            ("calmer", "Спокойнее"),
        ),
        # DRF-1747 — вопрос о самочувствии: здесь человек может не знать.
        escape=True,
    ),
)

GOAL_STEP_PROMPT = "Выбери цель — или напиши своими словами, чего хочешь."

# Полное число шагов прохода: цель + сужающие.
TOTAL_STEPS = 1 + len(ANKETA_STEPS)

_STEP_BY_KEY = {step.key: step for step in ANKETA_STEPS}

_ANSWERABLE_KEYS = frozenset({*_STEP_BY_KEY, GOAL_STEP_KEY})


def is_answerable_step(step_key: str) -> bool:
    """Известен ли серверу такой шаг вообще."""
    return step_key in _ANSWERABLE_KEYS


def narrowing_step(step_key: str) -> AnketaStep | None:
    """Сужающий шаг по ключу; шаг цели и незнакомые — ``None``."""
    return _STEP_BY_KEY.get(step_key)


def as_known_answer(
    step: AnketaStep,
    *,
    option_key: str | None,
    text: str | None,
    option_keys: list[str] | None = None,
) -> dict[str, Any]:
    """Ответ на шаг → строка блока «Уже учла» (DRF-1744), готовая к отрисовке.

    Макет C03 (DRF-1178): человек видит, что его услышали, и любой
    показанный факт может исправить. Поэтому строка несёт не только
    подпись выбранного, но и ``options`` шага: чтобы «Изменить» было
    чем ответить, экрану не нужен ни список вопросов, ни порядок —
    только этот шаг. ``prompt`` — голый вопрос, без пометки о влиянии
    (:func:`shown_prompt`): блок говорит «что ты сказал», не «зачем
    спросили». ``revisable`` — решение сервера, экран его не выводит.
    """
    labels = dict(step.options)
    if step.escape:
        labels[UNKNOWN_OPTION_KEY] = UNKNOWN_OPTION_LABEL
    chosen = list(option_keys or [])
    if chosen:
        # DRF-1746 — multi: одна строка «Уже учла» на шаг, подписи через
        # запятую в порядке вариантов шага, не в порядке тапов.
        label = ", ".join(labels[key] for key, _ in step.options if key in chosen)
    else:
        label = labels.get(option_key or "", text or option_key or "")
    return {
        "step": step.key,
        "prompt": step.prompt,
        "option_key": option_key,
        "option_keys": chosen,
        "label": label,
        "options": _wire_options(step),
        "mode": step.mode,
        "revisable": True,
        # DRF-1747 — «не знаю» показывается как сказанное, но известным
        # фактом не считается: при повторном проходе шаг задаётся заново
        # обычным вопросом, а не подтверждением.
        "unknown": option_key == UNKNOWN_OPTION_KEY,
        "origin": ORIGIN_ANKETA,
    }


def _wire_options(step: AnketaStep) -> list[dict[str, str]]:
    """Варианты шага для документа; ``unknown`` — последним и с ролью."""
    options = [{"key": key, "label": label} for key, label in step.options]
    if step.escape:
        options.append(
            {"key": UNKNOWN_OPTION_KEY, "label": UNKNOWN_OPTION_LABEL, "role": OPTION_ROLE_ESCAPE}
        )
    return options


def goal_step() -> AnketaStep:
    """Шаг цели: варианты — курируемые цели, свободный ввод открыт."""
    options = tuple(
        (option.key, option.label)
        for option in GoalOption.objects.filter(is_active=True)
    )
    return AnketaStep(
        key=GOAL_STEP_KEY,
        prompt=GOAL_STEP_PROMPT,
        options=options,
        allow_free_text=True,
        # Единственный шаг, ответ на который доезжает до выдачи:
        # goal_key → GoalOptionCategory → фильтр главной и полок.
        rule_ids=("goal.category_match",),
    )


def next_step(answered_keys: set[str]) -> AnketaStep | None:
    """Какой шаг задавать при уже отвеченных ``answered_keys``; ``None`` —
    спрашивать больше нечего, проход завершён.

    Порядок — единственный источник правды о последовательности, и он
    целиком здесь: цель первой, затем сужающие по списку. Проход,
    начатый до DRF-1764 (сужающие отвечены, цели нет), не мигрируется —
    он просто получает вопрос о цели и завершается штатно.
    """
    if GOAL_STEP_KEY not in answered_keys:
        return goal_step()
    for step in ANKETA_STEPS:
        if step.key not in answered_keys:
            return step
    return None


def step_index(step_key: str) -> int:
    """Человеческий номер шага, 1-based. Цель — первая."""
    if step_key == GOAL_STEP_KEY:
        return 1
    for index, step in enumerate(ANKETA_STEPS, start=2):
        if step.key == step_key:
            return index
    return TOTAL_STEPS


def is_last_step(step: AnketaStep, answered_keys: set[str]) -> bool:
    """Гарантирует ли сервер, что после этого шага вопросов не будет.

    Вычисляется тем же :func:`next_step`, что ведёт проход: последний —
    тот, после ответа на который спрашивать нечего. Не «index == total»:
    равенство чисел — совпадение, а не гарантия (DRF-1743).
    """
    return next_step(answered_keys | {step.key}) is None


def as_missing_item(
    step: AnketaStep, *, answered_keys: set[str], goal_key: str | None = None,
) -> dict[str, Any]:
    """Шаг → элемент ``missing``, готовый к отрисовке как есть.

    Форма расширяет DRF-1190, а не ломает его: ``kind`` и ``prompt`` на
    прежних местах, поэтому потребитель, читающий только их
    (``GoalInviteCard``), продолжает работать без правки.
    """
    item: dict[str, Any] = {
        "kind": MISSING_GOAL_ANKETA,
        "prompt": shown_prompt(step, goal_key),
        "step": step.key,
        "options": _wire_options(step),
        "allow_free_text": step.allow_free_text,
        # DRF-1746 — тип ответа; экран рисует компонент по нему.
        "mode": step.mode,
        "progress": {
            "index": step_index(step.key),
            "total": TOTAL_STEPS,
            "is_last": is_last_step(step, answered_keys),
        },
    }
    if step.mode == MODE_SCALE and step.scale_ends:
        item["scale"] = {"low_label": step.scale_ends[0], "high_label": step.scale_ends[1]}
    if step.mode == MODE_TEXT:
        item["text_limit"] = TEXT_ANSWER_LIMIT
    return item
