"""Минимальный Unified DecisionContext — эфемерный документ состояния.

Ответ 3 главного окна (2026-08-19): экран — тупой отрисовщик. Он получает
документ «что известно / чего не хватает / что предложить» и только
отображает его; ни одного решения на клиенте. Когда появится Decision
Orchestrator, меняется этот модуль (и слой над ним), экран не трогаем.

Инвариант контракта: **документ не содержит данных, из которых экран мог
бы вычислить другое содержимое** — ни флагов «показать X», ни сырых
списков, требующих фильтрации. Всё, что приходит, отображается как есть.

Документ эфемерен: в БД не хранится. Durable-факт — goals.ClientGoal,
проекция строится поверх него на каждый запрос.

DRF-1451 — версия 2: анкета
---------------------------

Решение владельца 03.09.2026 (поправка A-1 к BOT-001, §24): клиент без
цели получает **последовательность** вопросов, по завершении которой
цель сформирована. Последовательность целиком серверная — см.
``goals/anketa.py``; сюда она попадает по одному шагу за раз, готовым
элементом ``missing``. Экран по-прежнему не вычисляет ничего: он не
знает ни следующего вопроса, ни номера текущего — номер приходит в
``progress``.

Что версия 2 добавила к документу:

- ``known.anketa`` — ответы открытого прохода, блок «Уже учла»
  (DRF-1744): каждая строка несёт ``options`` шага и ``revisable``,
  чтобы «Изменить» отвечалось тем же ``POST /goals/select`` с
  ``answer.revise = true``;
- ``missing[].kind == "goal_anketa"`` — шаг анкеты; у него, помимо
  прежних ``kind``/``prompt``, есть ``step``, ``options``,
  ``allow_free_text`` и ``progress``. Старые kind'ы не тронуты, поэтому
  потребитель, читающий только ``prompt``, продолжает работать.
- ``next`` — куда вести человека, когда спрашивать больше нечего.
  Раньше этого решения не было ни у кого: экран после выбора цели просто
  перерисовывался. Теперь его принимает сервер, а не клиент.
- намерение ``start_anketa`` — вход в анкету для того, кто УЖЕ с целью
  (DRF-1225 / C-4: проходить сколько угодно раз).

Анкета — не ворота (условие C-2). ``formulate_own`` остаётся в документе
на каждом шаге: назвать услугу и уйти к подбору можно, не ответив ни на
один вопрос. И названная услуга **признаётся готовой целью** —
``_goal_is_resolved`` ниже — чтобы не уронить человека обратно в
уточнение.

DRF-2177 — C03 в живой путь (макет DRF-1178, решение владельца §60)
---------------------------------------------------------------------

Замер 20.09: у человека с целью документ вопросов не нёс — их клали только
при открытом проходе или без цели; вместо вопросов — семь чипов и «Найти
услугу». Теперь:

- цель есть, а сужающие шаги под неё не отвечены → первый сужающий шаг
  сразу (C03.2), шаг цели считается отвеченным самой целью; ответ создаёт
  проход, привязанный к активной цели;
- семь целей при активной цели скрывает экран — только за «Изменить»
  (``start_anketa`` → шаг цели с опциями); документ несёт ``suggestions``
  ради подписи цели у прежних читателей и ``known.goal.label`` для новых;
- ``next``: пока есть вопросы — ``None`` (честное молчание, а не кнопка в
  никуда); контекст собран — ``return_to_chat`` (C03.5 «Спасибо! Этого
  достаточно…» → чат). ``browse_catalog`` («Найти услугу») документ при
  включённой анкете больше не несёт — отступление от буквы C-2 по §60,
  вынесено владельцу; по сути C-2 держится: экран не корень (вход с H01,
  каталог в одном тапе назад), свободный ввод и «Не знаю» — на каждом шаге,
  названная услуга — контекст собран без единого вопроса.

Чат → вопросы («консьерж открывает вопросы при нехватке контекста») —
целевой UX-контракт, не runtime (ruling §61): вычисления «нехватки
контекста» нет, и здесь оно не изобретается. Входы — H01 и меню.

Флаг ``GOAL_ANKETA_ENABLED`` (умолчание ON) выключает анкету и
возвращает ровно документ DRF-1190.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.db.models.functions import Lower

from services.models import GoalOption

from . import anketa
from .direction import answers_for_goal, direction_for
from .lifecycle import OPEN_STATES
from .models import ClientGoal, GoalAnketaAnswer, GoalAnketaRun
from .service_match import match_named_service

if TYPE_CHECKING:
    from users.models import User

# Тексты — часть серверного контракта, не логика экрана (Ответ 3).
PROMPT_GOAL_MISSING = "Что хочешь изменить или как хочешь себя чувствовать?"
PROMPT_GOAL_CLARIFICATION = (
    "Записала: «{goal_text}». Расскажи чуть подробнее — "
    "что для тебя важнее всего?"
)
PROMPT_GOAL_GUIDANCE = (
    "Давай разберёмся вместе. Что сейчас беспокоит больше всего — "
    "тело, внешность, состояние?"
)

# Три намерения DRF-1190 — данными, чтобы экран не решал даже их состав.
INTENT_CHOOSE_SUGGESTED = "choose_suggested"
INTENT_FORMULATE_OWN = "formulate_own"
INTENT_NEED_GUIDANCE = "need_guidance"

INTENT_START_ANKETA = "start_anketa"

_INTENTS: list[dict[str, str]] = [
    {"id": INTENT_CHOOSE_SUGGESTED, "label": "Выбрать из предложенного"},
    # DRF-2177 — подпись по макету C03 (DRF-1178): «Рассказать своими словами».
    {"id": INTENT_FORMULATE_OWN, "label": "Рассказать своими словами"},
    {"id": INTENT_NEED_GUIDANCE, "label": "Не понимаю, чего хочу"},
]

# Намерение повторного прохода. Отдельно от ``_INTENTS``, потому что
# показывается не всегда: предлагать «пройти анкету заново» тому, кто
# сейчас в середине прохода, — предлагать бросить начатое.
INTENT_START_ANKETA_LABEL = "Пройти анкету заново"

# DRF-1451: куда вести, когда спрашивать больше нечего. Идентификатор, не
# путь: маршрут — контракт клиента (тот же приём, что у слагов бота в
# ``_ROUTE_MAP``), и сервер не обязан знать имена экранов мини-аппа.
NEXT_BROWSE_CATALOG = "browse_catalog"
NEXT_BROWSE_CATALOG_LABEL = "Найти услугу"

# DRF-2177 — контекст собран: назад в чат (макет C03.5, авто-переход к C04).
# Кадр «Спасибо! Этого достаточно, чтобы подобрать тебе подходящий шаг» —
# константа экрана (он знает этот id); ``label`` — для потребителя, который
# новый id ещё не знает и рисует ``next`` кнопкой.
NEXT_RETURN_TO_CHAT = "return_to_chat"
NEXT_RETURN_TO_CHAT_LABEL = "Вернуться в чат"

MISSING_GOAL = "goal"
MISSING_GOAL_CLARIFICATION = "goal_clarification"
MISSING_GOAL_GUIDANCE = "goal_guidance"


def _nutrition_goal_hints() -> dict[str, list[str]]:
    """DRF-2124 (План-B, В-4) — подсказки анкете питания под курируемые цели:
    данные шаблона плана (``wellness.PlanTemplate.nutrition_goal_hint``), не
    решение; один запрос на документ, не на цель. Импорт ленивый: wellness уже
    зависит от goals (FK плана на цель), обратная связь — только на вызов."""
    from wellness.plan_lite_templates import nutrition_goal_hints

    return nutrition_goal_hints()


def _goal_label(goal: ClientGoal, labels: dict[str, str]) -> str:
    """Человеческая подпись цели: свои слова, иначе label курируемой цели,
    иначе ключ — тот же порядок, что у экрана и главного (DRF-2177)."""
    if goal.goal_text:
        return goal.goal_text
    if goal.goal_key:
        return labels.get(goal.goal_key, goal.goal_key)
    return ""


def _goal_payload(
    goal: ClientGoal,
    hints: dict[str, list[str]] | None = None,
    labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    if hints is None:
        hints = _nutrition_goal_hints()
    if labels is None:
        labels = {s["key"]: s["label"] for s in _suggestions()}
    collected = answers_for_goal(goal)
    return {
        # DRF-1660: id и состояние — чтобы у цели был адрес для перехода
        # (``POST /goals/state/`` требует goal_id) и чтобы пауза была видна.
        "id": str(goal.id),
        "state": goal.state,
        "goal_key": goal.goal_key,
        "goal_text": goal.goal_text,
        # DRF-2177 — подпись цели едет С ЦЕЛЬЮ: до этого экран и главный
        # выводили её из ``suggestions[].label`` по ключу, то есть подпись
        # зависела от того, показан ли ряд целей. Ряд при цели теперь
        # скрыт (§60), а подпись обязана остаться.
        "label": _goal_label(goal, labels),
        # DRF-1772 (К-3) — WHAT карточки C04 и собранные ответы под эту цель
        # (WHY — grounded-пересказ, OD_C04 §1). ``direction`` — из
        # курируемой таблицы владельца (``services.GoalDirection``), ``None``
        # — направления нет, карточки не будет (C04.4 механически).
        "direction": direction_for(goal, collected),
        "answers": collected,
        "selected_at": goal.selected_at.isoformat(),
        "source_channel": goal.source_channel,
        # DRF-2124 — едет РЯДОМ с целью, к которой относится: читатель (анкета
        # питания в боте) подсвечивает вариант, не предвыбирает и не пропускает
        # шаг (§7.1/§5.1). Не гейтится PLAN_LITE_ENABLED — это данные о цели.
        # ``None`` — подсказки нет (§103), не пустой список.
        "nutrition_goal_hint": hints.get(goal.goal_key) if goal.goal_key else None,
    }


def _suggestions() -> list[dict[str, Any]]:
    """Активные курируемые подсказки — уже отфильтрованные и отсортированные."""
    return [
        {"key": option.key, "label": option.label}
        for option in GoalOption.objects.filter(is_active=True)
    ]


def _anketa_enabled() -> bool:
    return bool(getattr(settings, "GOAL_ANKETA_ENABLED", True))


def open_anketa_run(client: User) -> GoalAnketaRun | None:
    """Незакрытый проход анкеты клиента, если он есть."""
    return (
        GoalAnketaRun.objects.filter(client=client, completed_at__isnull=True)
        .order_by("-started_at")
        .first()
    )


def known_anketa_answers(run: GoalAnketaRun | None) -> list[dict[str, Any]]:
    """Ответы открытого прохода в порядке шагов — блок «Уже учла» (DRF-1744).

    Только открытый проход: пока идёт C03, человек видит, что уже
    сказал, и может поправить. Завершённый проход — уже цель, она
    живёт в ``known.goal``. Порядок — порядок ``ANKETA_STEPS``, не
    порядок записи: экран порядок не вычисляет. Ответы на неизвестные
    серверу шаги (снятые из ``ANKETA_STEPS``) не показываются — их
    нечем исправить.
    """
    if run is None:
        return []
    by_step = {
        answer.step_key: answer
        for answer in run.answers.all()
        if anketa.narrowing_step(answer.step_key) is not None
    }
    return [
        anketa.as_known_answer(
            step,
            option_key=by_step[step.key].option_key,
            text=by_step[step.key].answer_text,
            option_keys=list(by_step[step.key].option_keys or []),
        )
        for step in anketa.ANKETA_STEPS
        if step.key in by_step
    ]


def previous_answers(client: User) -> dict[str, GoalAnketaAnswer]:
    """Последний ответ человека на каждый сужающий шаг из ЗАВЕРШЁННЫХ
    проходов — то, что можно подтвердить вместо переспроса (DRF-1745).

    Только завершённые: открытый проход — это текущие вопросы, не прошлое.
    «Не знаю» (DRF-1747) известным не считается — шаг задаётся заново
    обычным вопросом. Незнакомые серверу шаги не показываются: их нечем
    ни подтвердить, ни изменить.
    """
    latest: dict[str, GoalAnketaAnswer] = {}
    rows = (
        GoalAnketaAnswer.objects.filter(run__client=client, run__completed_at__isnull=False)
        .order_by("-created_at")
    )
    for row in rows:
        if row.step_key in latest or anketa.narrowing_step(row.step_key) is None:
            continue
        if row.option_key == anketa.UNKNOWN_OPTION_KEY:
            continue
        latest[row.step_key] = row
    return latest


def known_value_of(step: anketa.AnketaStep, row: GoalAnketaAnswer) -> dict[str, Any]:
    """Прошлый ответ шага в форме ``known_value`` документа."""
    known = anketa.as_known_answer(
        step,
        option_key=row.option_key,
        text=row.answer_text,
        option_keys=list(row.option_keys or []),
    )
    return {
        "option_key": row.option_key,
        "option_keys": known["option_keys"],
        "text": row.answer_text,
        "label": known["label"],
    }


def answered_step_keys(run: GoalAnketaRun | None) -> set[str]:
    """Ключи отвеченных шагов прохода; без прохода — пусто.

    DRF-2177: проход, привязанный к цели (``run.goal``), считает шаг цели
    отвеченным — целью он и отвечен. Иначе проход «от цели» (первый
    сужающий вопрос при уже выбранной цели) не завершился бы никогда:
    ``next_step`` снова и снова просил бы цель, которая есть.
    """
    if run is None:
        return set()
    keys = set(run.answers.values_list("step_key", flat=True))
    if run.goal_id is not None:
        keys.add(anketa.GOAL_STEP_KEY)
    return keys


def goal_context_collected(goal: ClientGoal) -> bool:
    """Собран ли контекст ПОД ЭТУ цель — то есть спрашивать больше нечего.

    Да, когда все сужающие шаги отвечены в завершённом проходе этой цели
    (ответ «Не знаю» — тоже ответ, DRF-1747), либо когда в тексте цели
    названа услуга (C-2: «назвал услугу — к подбору», вопросов не было и
    не будет). Прямой выбор цели чипом закрывает открытый проход
    (`api._close_open_run`) БЕЗ сужающих ответов — поэтому «завершённый
    проход есть» само по себе ничего не значит, считаются ответы.

    Под цель, а не под человека: сменил цель — вопросы задаются снова
    (прошлые ответы приходят подтверждением, DRF-1745).
    """
    text = (goal.goal_text or "").strip()
    if text and match_named_service(text) is not None:
        return True
    answered = set(
        GoalAnketaAnswer.objects.filter(run__goal=goal, run__completed_at__isnull=False)
        .values_list("step_key", flat=True)
    )
    return all(step.key in answered for step in anketa.ANKETA_STEPS)


def asks_from_goal(client: User, active_goal: ClientGoal | None) -> bool:
    """Задаёт ли документ первый сужающий шаг «от цели» (DRF-2177).

    Одно условие для документа и для API: цель есть, она разрешена
    (не ждёт уточнения) и контекст под неё не собран. Считай API его
    шире — протухший ответ на сужающий шаг после собранного контекста
    создавал бы новый проход вместо 409 (C-1: последовательность
    серверная, протухший экран получает отказ).
    """
    if active_goal is None or not _anketa_enabled():
        return False
    if not _goal_is_resolved(active_goal, service_match=True):
        return False
    return not goal_context_collected(active_goal)


def next_anketa_step(run: GoalAnketaRun | None) -> anketa.AnketaStep | None:
    """Какой шаг задавать сейчас. ``run is None`` — проход ещё не начат;
    результат ``None`` — спрашивать больше нечего."""
    return anketa.next_step(answered_step_keys(run))


def _goal_is_resolved(goal: ClientGoal, *, service_match: bool = True) -> bool:
    """Считается ли цель готовой — то есть уточнять больше нечего.

    Ключ курируемой подсказки готов всегда. Свободный текст готов, если
    в нём **названо** что-то известное: точное совпадение с label цели
    (прежняя семантика ``goals.resolution``) или имя услуги/категории из
    каталога (DRF-1451).

    Ради второго всё и затевалось: без него человек, написавший «хочу
    маникюр», получал ``goal_clarification`` — то есть падал обратно в
    вопросы ровно там, где владелец велел вести к подбору.

    Каталог читается целиком, по всем салонам (DRF-1472). Салон в этом
    решении не участвует и на цели не хранится: цели спрашивает
    клиентский бот-витрина, салона у него нет, и услуга, которая есть
    хоть у одного салона, человеку доступна. Ровно так же — и по той же
    причине — читается ``GoalOption``: подсказки целей глобальны.

    ``service_match=False`` выключает именно эту, новую половину.
    Приходит из ``GOAL_ANKETA_ENABLED``: рубильник отката обязан
    откатывать ВСЁ, что приехало с DRF-1451, включая сканирование
    каталога, — иначе выключить его из-за нагрузки или плохого
    совпадения было бы нечем.
    """
    if goal.goal_key:
        return True
    text = (goal.goal_text or "").strip()
    if not text:
        return False
    exact_label = (
        GoalOption.objects.filter(is_active=True)
        .annotate(label_lower=Lower("label"))
        .filter(label_lower=text.casefold())
        .exists()
    )
    if exact_label:
        return True
    if not service_match:
        return False
    return match_named_service(text) is not None


def build_decision_context(
    client: User,
    *,
    guidance: bool = False,
) -> dict[str, Any]:
    """Собрать документ состояния для клиента.

    ``guidance=True`` — ответ на намерение «не понимаю, чего хочу»:
    состояние ведения, в котором Ayla задаёт первый вопрос. Эфемерно
    (Ответ 3: ведущий сценарий — следующий проход; состояние не пишется
    в ClientGoal, потому что цели ещё нет).
    """
    active_goal = (
        ClientGoal.objects.filter(client=client, state=ClientGoal.State.ACTIVE)
        .order_by("-selected_at")
        .first()
    )
    # DRF-1660: открытые цели (ACTIVE + PAUSED) списком, аддитивно к
    # скаляру ``known.goal``. Скаляр не переводится на список намеренно
    # (§97 OD-GOAL-E: двенадцать его потребителей механически не
    # переводятся); список нужен, чтобы у цели на паузе был выход —
    # человек видит её и может снять с паузы (§122).
    open_goals = list(
        ClientGoal.objects.filter(client=client, state__in=OPEN_STATES)
        .order_by("-selected_at")
    )

    anketa_on = _anketa_enabled()
    run = open_anketa_run(client) if anketa_on else None

    hints = _nutrition_goal_hints()  # DRF-2124 — один запрос на документ
    suggestions = _suggestions()  # один запрос: и ряд целей, и подписи
    labels = {s["key"]: s["label"] for s in suggestions}
    known: dict[str, Any] = {
        "goal": _goal_payload(active_goal, hints, labels) if active_goal else None,
        "goals": [_goal_payload(goal, hints, labels) for goal in open_goals],
        # DRF-1744: что человек уже сказал в этом проходе — аддитивно.
        "anketa": known_anketa_answers(run),
    }

    missing: list[dict[str, Any]] = []
    if guidance:
        missing.append({"kind": MISSING_GOAL_GUIDANCE, "prompt": PROMPT_GOAL_GUIDANCE})
    elif anketa_on and (run is not None or active_goal is None):
        # DRF-1451. Открытый проход ведём до конца независимо от того,
        # есть ли уже цель: повторный проход (C-4) начинается именно так.
        # Прохода нет и цели нет — задаём первый вопрос (с DRF-1764 это
        # цель); строка прохода появится на первом ответе, потому что GET
        # не пишет в БД. Формулировка сужающего шага — под цель прохода
        # (``run.goal``), а не под любую активную: цель прохода и есть та,
        # под которую задаются вопросы.
        step = next_anketa_step(run)
        if step is not None:
            run_goal_key = run.goal.goal_key if run is not None and run.goal_id else None
            # DRF-1745 — повторный проход: сужающий шаг, на который человек
            # уже отвечал, приходит подтверждением, а не переспросом.
            # Первый проход без прошлого — как раньше.
            previous = previous_answers(client).get(step.key) if run is not None else None
            missing.append(anketa.as_missing_item(
                step,
                answered_keys=answered_step_keys(run),
                goal_key=run_goal_key,
                known_value=known_value_of(step, previous) if previous is not None else None,
            ))
    elif active_goal is None:
        missing.append({"kind": MISSING_GOAL, "prompt": PROMPT_GOAL_MISSING})
    elif not _goal_is_resolved(active_goal, service_match=anketa_on):
        # Свободный текст, в котором ничего не названо: уверенность
        # низкая — уточняем, а не отображаем насильно в ближайший чип
        # (OD-1). Названная услуга сюда не попадает (DRF-1451).
        missing.append({
            "kind": MISSING_GOAL_CLARIFICATION,
            "prompt": PROMPT_GOAL_CLARIFICATION.format(
                goal_text=(active_goal.goal_text or "")[:200],
            ),
        })
    elif asks_from_goal(client, active_goal):
        # DRF-2177 — C03.2 «первый вопрос — показываем сразу». Цель есть
        # (выбрана чипом или названа), прохода нет, сужающие шаги под неё
        # не отвечены: шаг цели считается отвеченным самой целью, и человек
        # видит первый сужающий вопрос. Проход появится на первом ответе
        # (GET не пишет в БД) и привяжется к этой цели (`api._answer_anketa`).
        step = anketa.next_step({anketa.GOAL_STEP_KEY})
        if step is not None:
            previous = previous_answers(client).get(step.key)
            missing.append(anketa.as_missing_item(
                step,
                answered_keys={anketa.GOAL_STEP_KEY},
                goal_key=active_goal.goal_key,
                known_value=known_value_of(step, previous) if previous is not None else None,
            ))

    intents = list(_INTENTS)
    if anketa_on and run is None and active_goal is not None:
        # Повторный проход предлагаем только тому, кто уже с целью и
        # сейчас не в середине анкеты (DRF-1225 / C-4).
        intents.append(
            {"id": INTENT_START_ANKETA, "label": INTENT_START_ANKETA_LABEL}
        )

    # Куда человек может уйти отсюда. Называет сервер, а не экран.
    #
    # Присутствует ВСЕГДА, а не только когда спрашивать нечего, — и это
    # исправление, а не украшение. С условием «не осталось вопросов»
    # получалось следующее: поверхность цели монтируется на корне, где
    # кнопки «назад» нет (её там и не должно быть) и нижней навигации у
    # клиента нет тоже. Пока в `missing` был хоть один вопрос, `next`
    # молчал — и уйти с экрана было нельзя иначе, чем создав цель.
    # То есть анкета становилась воротами, что запрещено и решением
    # владельца (условие C-2), и non-goal #1 BOT-001, который владелец
    # НЕ отменял: «Ayla MUST NOT require a guided onboarding sequence
    # before useful action».
    #
    # Хуже всего это било по тому, ради кого правка и делалась: человек
    # писал «хочу маникюра» (родительный падеж — имя каталога дословно
    # не совпадает), получал `goal_clarification`, и `next` замолкал.
    # Он назвал услугу — и остался заперт на вопросе.
    #
    # Формулировка нарочно ничего не обещает про подбор ПОД ЦЕЛЬ:
    # GOAL_RESOLUTION_ENABLED на пилоте выключен, и обещание было бы
    # ложью до его включения. «Найти услугу» правдиво в обоих случаях.
    #
    # DRF-2177 (§60) — абзац выше описывает документ при ВЫКЛЮЧЕННОЙ
    # анкете, он не тронут. При включённой «Найти услугу» с экрана уходит:
    # пока есть вопросы — `next` молчит честно (`None`, не кнопка в
    # никуда; выход человека — «назад» на H01, свободный ввод, «Не знаю»);
    # контекст собран — `return_to_chat` (C03.5). Отступление от буквы
    # C-2 по §60 — вынесено владельцу.
    next_step_hint: dict[str, str] | None
    if not anketa_on:
        next_step_hint = {"id": NEXT_BROWSE_CATALOG, "label": NEXT_BROWSE_CATALOG_LABEL}
    elif missing:
        next_step_hint = None
    else:
        next_step_hint = {"id": NEXT_RETURN_TO_CHAT, "label": NEXT_RETURN_TO_CHAT_LABEL}

    # На шаге цели сам шаг УЖЕ несёт курируемые цели своими options —
    # из того же queryset, что и suggestions. Оставить обе секции
    # значило бы нарисовать человеку два одинаковых ряда чипов с
    # одинаковыми подписями, отправляющих разные тела с одинаковым
    # исходом. Выход при этом не теряется: чипы шага создают цель ровно
    # так же, и свободный ввод на шаге цели открыт.
    #
    # DRF-2177 (§60): «семь целей только за „Изменить"» при активной цели
    # держит ЭКРАН (он знает, что цель известна), а не документ: ряд
    # остаётся в документе, потому что по нему потребители — этот экран и
    # главный бота — до сих пор выводят подпись цели по ключу; обнули мы
    # его здесь, у цели на главном вылез бы сырой ключ до выкладки бота.
    # Подпись теперь едет и в ``known.goal.label`` — когда все читатели
    # перейдут на неё, ряд при цели можно снять и здесь.
    on_goal_step = bool(missing) and missing[0].get("step") == anketa.GOAL_STEP_KEY

    return {
        "version": 2,
        "known": known,
        "missing": missing,
        "suggestions": [] if on_goal_step else suggestions,
        "intents": intents,
        "next": next_step_hint,
    }
