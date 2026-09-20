"""Plan Lite без веса — писатель и чтение (DRF-2101, решение владельца §49).

План из 1–3 обязательств-ДЕЙСТВИЙ из уже данной цели (``goals.ClientGoal``):
записаться на услугу под цель / дневник N дней в неделю / вода N раз в день.
Человек видит только adherence за ТЕКУЩЕЕ ведро каденса — «N из M» — и
ничего о результате (В-5, DRF-1332): ни процента цели, ни шкалы, ни итога
по плану.

**Ни одного наблюдения тела.** Этот модуль не знает про
``body_observation_gate``, ``record_observation``, ``ProgressObservation``
и вес — не по договорённости, а по переписи импортов
(``wellness/tests/test_plan_lite_2101.py``). Гейт O (§152) к плану без
наблюдений не относится: хранить нечего.

**Цель держит сам план** (``PersonalPlan.goal`` + ``goal_key`` снимком), а
не ``DesiredOutcome``: тот пишется только через ``record_outcome`` под
гейтом D (GOALS-R6), и §49 санкционировал план из действий, не хранение
результата. ``PlanOutcomeLink`` у планов Plan Lite нет —
``compute_plan_adherence`` на это не опирается.

Согласие: план — производная от цели, данной под своим основанием, и от
уже согласованных журналов; нового scope здесь не заводится. Отказ по
согласию дневника для ``log_food``/``log_water`` — на стороне записи
(канонический поток DRF-1968), не здесь: план лишь называет обязательство.

Флаг ``PLAN_LITE_ENABLED`` (default false) читают и писатель, и чтение.

DRF-2123 (§51, План-A): предложение плана по активной цели из курируемого
шаблона (``wellness.PlanTemplate`` — данные, не код; ``propose_plan``), и
``template_version`` в писателе → ``PersonalPlan.source``. Каденс
``per_2_weeks``: ведро 14 дней от создания плана.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from goals.models import ClientGoal

# DRF-2124 (План-B) — «день в ориентире» нуждается в ориентире, а ориентир
# — профиль питания, которого этот модуль не читает (В-1, перепись импортов
# ``plan_lite*.py``). Факт приходит ФУНКЦИЕЙ из ``nutrition`` (В-2), без
# имени модели здесь.
from nutrition.services.plan_facts import count_days_within_calorie_target

from .fact_providers import count_fact_days, count_facts
from .models import PersonalPlan, PlanAction
from .plan_lite_templates import active_template_for, template_version_exists

MIN_ACTIONS = 1
MAX_ACTIONS = 3
MAX_TARGET_COUNT = 14


def plan_lite_enabled() -> bool:
    return bool(getattr(settings, "PLAN_LITE_ENABLED", False))


class PlanLiteError(Exception):
    """Base for Plan Lite refusals."""


class PlanLiteDisabled(PlanLiteError):
    """Флаг выключен — штатный отказ, не сбой."""


class GoalNotFound(PlanLiteError):
    """Нет такой активной цели у этого человека — чужая или закрытая:
    «не найдено», не «чужое», чтобы по коду нельзя было перебирать."""


class PlanAlreadyActive(PlanLiteError):
    """0..1 ACTIVE на человека (OD-GOAL-4): сменить — только закрыв прежний."""


class NoActivePlan(PlanLiteError):
    """Закрывать нечего."""


class InvalidActions(PlanLiteError):
    """1–3 действия, курируемый ключ, каденс, положительный target."""


class TemplateNotFound(PlanLiteError):
    """Нет активного шаблона для цели (DRF-2123) — 404 ``no_template``,
    НЕ пустой план: предложение без обязательств ничего не предлагает."""


class InvalidTemplateVersion(PlanLiteError):
    """``template_version`` не целое ≥ 1 или такой версии у цели нет — 400."""


@dataclass(frozen=True)
class ActionSpec:
    action_type: str
    cadence: str
    target_count: int


_ACTION_TYPES = {choice.value for choice in PlanAction.ActionType}
_CADENCES = {choice.value for choice in PlanAction.Cadence}


def parse_actions(raw: Any) -> list[ActionSpec]:
    """Форма 1–3 обязательств; всё лишнее — отказ, не молчаливая правка."""
    if not isinstance(raw, list) or not (MIN_ACTIONS <= len(raw) <= MAX_ACTIONS):
        raise InvalidActions(f"actions must be a list of {MIN_ACTIONS}..{MAX_ACTIONS} items")
    specs: list[ActionSpec] = []
    for item in raw:
        if not isinstance(item, dict):
            raise InvalidActions("each action must be an object")
        action_type = item.get("action_type")
        cadence = item.get("cadence")
        target = item.get("target_count", 1)
        if action_type not in _ACTION_TYPES:
            raise InvalidActions(f"unknown action_type; allowed: {sorted(_ACTION_TYPES)}")
        if cadence not in _CADENCES:
            raise InvalidActions(f"unknown cadence; allowed: {sorted(_CADENCES)}")
        if isinstance(target, bool) or not isinstance(target, int) or not (1 <= target <= MAX_TARGET_COUNT):
            raise InvalidActions(f"target_count must be an integer 1..{MAX_TARGET_COUNT}")
        specs.append(ActionSpec(action_type=action_type, cadence=cadence, target_count=target))
    if len({s.action_type for s in specs}) != len(specs):
        raise InvalidActions("each action_type at most once per plan")
    return specs


def _active_plan(user) -> PersonalPlan | None:
    return (
        PersonalPlan.objects.filter(user=user, status=PersonalPlan.Status.ACTIVE)
        .prefetch_related("actions")
        .first()
    )


def parse_template_version(raw: Any) -> int | None:
    """``template_version`` необязателен; если дан — целое ≥ 1 (bool — не
    целое), иначе отказ формы."""
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise InvalidTemplateVersion("template_version must be an integer >= 1")
    return raw


def create_plan(
    user,
    *,
    actions: list[ActionSpec],
    goal_id: UUID | None = None,
    template_version: int | None = None,
) -> PersonalPlan:
    """``goal_id`` необязателен (PR-1b): без него план строится от АКТИВНОЙ
    цели вызывающего — у клиента она одна по схеме
    (``clientgoal_one_active_per_client``), и экран Mini App её id не видит
    (decision-context отдаёт ключ и текст, не id). Нет активной цели —
    «не найдено»: план без цели не бывает. С ``goal_id`` — как прежде:
    он обязан быть активной целью вызывающего.

    ``template_version`` (DRF-2123): версия шаблона этой цели, по которому
    человек согласился на предложение — любая существующая (и снятая: план
    по прежней версии помечен законно); иначе ``InvalidTemplateVersion``.
    Действия при этом всё равно приходят телом запроса — шаблон только
    источник, не приказ: бот мог дать человеку убрать одно из трёх."""
    if not plan_lite_enabled():
        raise PlanLiteDisabled()
    goals = ClientGoal.objects.filter(client=user, state=ClientGoal.State.ACTIVE)
    goal = (goals.filter(pk=goal_id) if goal_id is not None else goals).first()
    if goal is None:
        raise GoalNotFound(str(goal_id) if goal_id is not None else "no_active_goal")
    source = "manual"
    if template_version is not None:
        if not template_version_exists(goal.goal_key, template_version):
            raise InvalidTemplateVersion(
                f"no template version {template_version} for goal {goal.goal_key!r}"
            )
        source = f"template:{goal.goal_key}:v{template_version}"
    with transaction.atomic():
        if _active_plan(user) is not None:
            raise PlanAlreadyActive()
        try:
            plan = PersonalPlan.objects.create(
                user=user, goal=goal, goal_key=goal.goal_key or "", source=source,
            )
        except IntegrityError as exc:  # гонка двух POST — ловит частичная уникальность
            raise PlanAlreadyActive() from exc
        for spec in actions:
            PlanAction.objects.create(
                plan=plan,
                action_type=spec.action_type,
                cadence=spec.cadence,
                target_count=spec.target_count,
            )
    return plan


def close_plan(user) -> PersonalPlan:
    """Закрыть активный план — append-only: строка и её обязательства остаются."""
    if not plan_lite_enabled():
        raise PlanLiteDisabled()
    with transaction.atomic():
        plan = (
            PersonalPlan.objects.select_for_update()
            .filter(user=user, status=PersonalPlan.Status.ACTIVE)
            .first()
        )
        if plan is None:
            raise NoActivePlan()
        plan.status = PersonalPlan.Status.CLOSED_BY_USER
        plan.closed_at = timezone.now()
        plan.save(update_fields=["status", "closed_at"])
    return plan


# ─── предложение плана из шаблона (DRF-2123) ─────────────────────────────────


def propose_plan(user) -> dict[str, Any]:
    """Предложение по активной цели вызывающего из активного шаблона
    (``PlanTemplate``). Ничего не создаёт и не пишет. Нет цели —
    ``GoalNotFound``; нет активного шаблона — ``TemplateNotFound`` (не
    пустой план)."""
    if not plan_lite_enabled():
        raise PlanLiteDisabled()
    goal = ClientGoal.objects.filter(client=user, state=ClientGoal.State.ACTIVE).first()
    if goal is None:
        raise GoalNotFound("no_active_goal")
    template = active_template_for(goal.goal_key)
    if template is None:
        raise TemplateNotFound(goal.goal_key or "")
    return {
        "goal_key": template.goal_key,
        "why": template.why_text,
        "template_version": template.version,
        "actions": [
            {
                "action_type": a["action_type"],
                "cadence": a["cadence"],
                "target_count": a["target_count"],
            }
            for a in template.actions
        ],
    }


# ─── чтение: adherence за текущее ведро ──────────────────────────────────────

_TWO_WEEKS = 14


def _current_bucket(cadence: str, today: date, *, anchor: date | None = None) -> tuple[date, date]:
    """[start, end) текущего ведра: день для per_day, неделя пн–вс для
    per_week, 14 дней от ``anchor`` (дата создания плана) для per_2_weeks —
    k-е ведро, содержащее «сейчас». Без ``anchor`` per_2_weeks отсчитывается
    от ``today``."""
    if cadence == PlanAction.Cadence.PER_WEEK:
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if cadence == PlanAction.Cadence.PER_2_WEEKS:
        anchor = anchor or today
        k = (today - anchor).days // _TWO_WEEKS
        start = anchor + timedelta(days=k * _TWO_WEEKS)
        return start, start + timedelta(days=_TWO_WEEKS)
    return today, today + timedelta(days=1)


def _aware(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time.min))


def _done_count(action: PlanAction, user_id: UUID, goal_key: str, start: date, end: date) -> int:
    """Только факты действий, не больше target: «3 из 3», не «7 из 3».

    per_week / per_2_weeks для журналов — ДНИ с записью (семь записей в один
    день — один день); per_day — записи за день; брони — по провайдеру
    (созданные в ведре).
    """
    b_start, b_end = _aware(start), _aware(end)
    if (
        action.cadence != PlanAction.Cadence.PER_DAY
        and action.action_type in (PlanAction.ActionType.LOG_FOOD, PlanAction.ActionType.LOG_WATER)
    ):
        facts = count_fact_days(action.action_type, user_id, b_start, b_end)
    else:
        facts = count_facts(action.action_type, user_id, b_start, b_end, goal_key=goal_key or None)
    return min(facts, action.target_count)


def _within_target_count(action: PlanAction, user_id: UUID, start: date, end: date) -> int | None:
    """DRF-2124 — ДНИ ведра с записями еды, чья сумма ≤ действующему ориентиру
    по калориям; ``None`` — ориентира нет (§103: не 0). Только для ``log_food``
    — у воды и записи такого факта нет.

    Единица — всегда день. Для недельных вёдер она совпадает с ``done_count``
    (там тоже дни) и обрезается тем же ``target_count``: «в ориентире 6 из 5»
    не бывает. Для ``per_day`` ``done_count`` считает ЗАПИСИ, а здесь ведро —
    один день, поэтому значение ∈ {0, 1}: «сегодня в ориентире» или нет; трёх
    записей по 300 ккал это не делает «3 в ориентире».
    """
    within = count_days_within_calorie_target(user_id, _aware(start), _aware(end))
    if within is None:
        return None
    cap = 1 if action.cadence == PlanAction.Cadence.PER_DAY else action.target_count
    return min(within, cap)


def plan_lite_payload(user, *, today: date | None = None) -> dict[str, Any] | None:
    """Документ ``plan_lite`` для wellness-context: ``None`` — плана нет или
    флаг выключен. Ключи — только форма обязательства и факты (В-5); у
    ``log_food`` — ещё ``within_target_count`` (DRF-2124), тоже факт: целое
    или ``null``, без производных."""
    if not plan_lite_enabled():
        return None
    plan = _active_plan(user)
    if plan is None:
        return None
    today = today or timezone.localdate()
    anchor = timezone.localdate(plan.created_at)
    actions = []
    for action in plan.actions.all():
        start, end = _current_bucket(action.cadence, today, anchor=anchor)
        payload: dict[str, Any] = {
            "action_type": action.action_type,
            "cadence": action.cadence,
            "target_count": action.target_count,
            "done_count": _done_count(action, plan.user_id, plan.goal_key, start, end),
            "bucket": {"start": start.isoformat(), "end": end.isoformat()},
        }
        if action.action_type == PlanAction.ActionType.LOG_FOOD:
            payload["within_target_count"] = _within_target_count(action, plan.user_id, start, end)
        actions.append(payload)
    return {"plan_id": str(plan.id), "goal_key": plan.goal_key or None, "actions": actions}
