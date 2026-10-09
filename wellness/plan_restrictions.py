"""Plan Engine — ограничения плана: стойкий незакрытый вопрос (DRF-2877).

Решение владельца 08.10.2026 (сквозная проверка Плана):

* ``CLARIFY`` — конкретный незакрытый вопрос, который НЕ исчезает просто после
  следующего сообщения;
* ограничение затрагивает зависимый шаг — или весь план, если относится ко
  всему плану (область — по области причины);
* черновик при ``CLARIFY`` можно сохранить, сохранив ограничения.

Разделение ролей. Вопрос задаёт, держит и оценивает БОТ: он же решает, что
вопрос закрыт. Каталог ХРАНИТ ограничение при плане и ОТКАЗЫВАЕТ действиям с
шагом, пока оно открыто. Текста вопроса и слов человека здесь нет — только
идентификаторы.

Что ограничение блокирует: переход шага к услуге, связь шага с записью и
запись от шага. Что не блокирует никогда: просмотр плана, паузу и архив.

Причины и условия снятия — закрытая таблица :data:`CAUSES`. Сегодня она ПУСТА,
и это намеренно. Владелец 08.10: «Универсальный вопрос CLARIFY без причины
придумывать не будем» — ограничение открывает КОНКРЕТНАЯ причина (состояние
расспроса о здоровье, условия услуги), а не сам вердикт «уточнить». Какие
причины существуют, какую область каждая закрывает и чем снимается, решает
владелец по таблице «состояние → условие снятия»; до решения причин нет, и
подставлять их нельзя. Механизм при этом полон и проверен узлами на
подставленных в узле причинах. Срока жизни у ограничения нет: источник правды
«вопрос ещё открыт» — эта таблица, а не память разговора.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from .models import Plan, PlanRestriction, PlanRestrictionLift
from .plan_engine import PlanEngineDisabled, PlanEngineError, PlanNotFound, plan_engine_enabled
from .plan_safety import SafetyInput

SCOPE_PLAN = PlanRestriction.Scope.PLAN
SCOPE_STEP = PlanRestriction.Scope.STEP

#: Человек ответил на заданный вопрос; что считать ответом, решает бот.
LIFT_ANSWERED = "answered"


@dataclass(frozen=True)
class Cause:
    #: Области, в которых причина может быть открыта.
    scopes: frozenset[str]
    #: Чем причина снимается. Пусто — условия снятия нет.
    lift_kinds: frozenset[str]


#: Закрытая таблица причин. Пуста до решения владельца: новая строка — только
#: по его слову о состоянии, его области и условии снятия. Универсальной
#: причины «вердикт „уточнить“» здесь нет и быть не должно.
CAUSES: dict[str, Cause] = {}

QUESTION_ID_MAX = 128


class RestrictionMalformed(PlanEngineError):
    """Описание ограничения не конформно. ``reason`` — машинное имя."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class RestrictionNotFound(PlanEngineError):
    pass


class RestrictionNotLiftable(PlanEngineError):
    """Снятие не принято. ``reason`` — машинное имя условия."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class RestrictionSpec:
    scope: str
    step_id: str
    cause: str
    question_id: str


def parse_restriction(raw: Any) -> RestrictionSpec:
    """``{scope, step_id?, cause, question_id}``. Всё неконформное — отказ."""
    if not isinstance(raw, dict):
        raise RestrictionMalformed("restriction_not_object")
    scope = raw.get("scope")
    if scope not in (SCOPE_PLAN, SCOPE_STEP):
        raise RestrictionMalformed("restriction_scope_invalid", str(scope))
    cause = raw.get("cause")
    known = CAUSES.get(cause) if isinstance(cause, str) else None
    if known is None:
        raise RestrictionMalformed("restriction_cause_unknown", str(cause))
    if scope not in known.scopes:
        raise RestrictionMalformed("restriction_scope_not_for_cause", f"{cause}:{scope}")
    step_id = raw.get("step_id")
    if scope == SCOPE_STEP:
        if not isinstance(step_id, str) or not step_id.strip():
            raise RestrictionMalformed("restriction_step_id_missing")
        step_id = step_id.strip()
    else:
        if step_id not in (None, ""):
            raise RestrictionMalformed("restriction_step_id_on_plan_scope")
        step_id = ""
    question_id = raw.get("question_id")
    if not isinstance(question_id, str) or not question_id.strip() or len(question_id.strip()) > QUESTION_ID_MAX:
        raise RestrictionMalformed("restriction_question_id_missing")
    return RestrictionSpec(scope=scope, step_id=step_id, cause=cause, question_id=question_id.strip())


def parse_restrictions(raw: Any) -> tuple[RestrictionSpec, ...]:
    """Список ограничений из команды сохранения; отсутствие поля — пусто."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RestrictionMalformed("restrictions_malformed")
    specs = tuple(parse_restriction(item) for item in raw)
    if len({(s.scope, s.step_id, s.question_id) for s in specs}) != len(specs):
        raise RestrictionMalformed("restriction_duplicate")
    return specs


# ─── чтение ──────────────────────────────────────────────────────────────────


def open_restrictions(plan: Plan):
    """Незакрытые ограничения плана, в порядке открытия."""
    return PlanRestriction.objects.filter(plan=plan, lift__isnull=True).order_by("created_at", "id")


def blocking_question_ids(plan: Plan, step_id: str) -> list[str]:
    """Вопросы, из-за которых шаг сейчас не исполним: открытые ограничения на
    всём плане и на самом шаге. Пусто — ограничений нет."""
    rows = open_restrictions(plan)
    return [r.question_id for r in rows if r.scope == SCOPE_PLAN or r.step_id == step_id]


def restrictions_document(plan: Plan) -> list[dict[str, Any]]:
    """Все ограничения плана, открытые и снятые, — как они были: история не
    переписывается."""
    out: list[dict[str, Any]] = []
    rows = PlanRestriction.objects.filter(plan=plan).select_related("lift").order_by("created_at", "id")
    for row in rows:
        lift = getattr(row, "lift", None)
        out.append(
            {
                "restriction_id": str(row.id),
                "scope": row.scope,
                "step_id": row.step_id or None,
                "cause": row.cause,
                "question_id": row.question_id,
                "opened_at": row.created_at.isoformat(),
                "lifted": None if lift is None else {
                    "lift_kind": lift.lift_kind, "lifted_at": lift.created_at.isoformat(),
                },
            }
        )
    return out


# ─── запись ──────────────────────────────────────────────────────────────────


def _step_ids(plan: Plan) -> set[str]:
    return {s.get("step_id") for s in plan.current_revision.steps_snapshot}


def create_restriction(plan: Plan, spec: RestrictionSpec, safety: SafetyInput) -> tuple[PlanRestriction, bool]:
    """Открыть ограничение на плане. Вызывается ВНУТРИ транзакции с запертым
    планом. То же открытое ограничение (область, шаг, вопрос) — одна строка:
    повтор возвращает её же, без дубля."""
    if spec.scope == SCOPE_STEP and spec.step_id not in _step_ids(plan):
        raise RestrictionMalformed("restriction_step_not_in_plan", spec.step_id)
    existing = open_restrictions(plan).filter(
        scope=spec.scope, step_id=spec.step_id, question_id=spec.question_id,
    ).first()
    if existing is not None:
        return existing, False
    row = PlanRestriction.objects.create(
        plan=plan,
        scope=spec.scope,
        step_id=spec.step_id,
        cause=spec.cause,
        question_id=spec.question_id,
        safety_state=safety.state,
        safety_policy_version=safety.policy_version,
        safety_evaluated_at_revision=safety.evaluated_at_revision,
    )
    return row, True


def _locked_plan(user, plan_id: UUID) -> Plan:
    plan = (
        Plan.objects.select_for_update(of=("self",))
        .select_related("goal", "current_revision")
        .filter(pk=plan_id, subject_user=user)
        .first()
    )
    if plan is None:
        raise PlanNotFound(str(plan_id))
    return plan


def open_restriction(user, plan_id: UUID, spec: RestrictionSpec, safety: SafetyInput) -> tuple[PlanRestriction, bool]:
    """Открыть ограничение на уже сохранённом плане. Вердикт хода здесь не
    гейт: ограничить план можно при любом состоянии, в том числе при «стоп» —
    ограничение только сужает."""
    if not plan_engine_enabled():
        raise PlanEngineDisabled()
    with transaction.atomic():
        plan = _locked_plan(user, plan_id)
        return create_restriction(plan, spec, safety)


def lift_restriction(
    user, plan_id: UUID, restriction_id: UUID, *, lift_kind: str, answer_option_id: str, safety: SafetyInput,
) -> tuple[PlanRestrictionLift, bool]:
    """Снять ограничение. Повтор — та же строка снятия, без второй."""
    if not plan_engine_enabled():
        raise PlanEngineDisabled()
    with transaction.atomic():
        plan = _locked_plan(user, plan_id)
        row = PlanRestriction.objects.filter(pk=restriction_id, plan=plan).first()
        if row is None:
            raise RestrictionNotFound(str(restriction_id))
        existing = PlanRestrictionLift.objects.filter(restriction=row).first()
        if existing is not None:
            return existing, False
        # Снятие расширяет доступное человеку — в отличие от открытия, оно
        # обязано идти при неблокирующем вердикте хода.
        if safety.blocks:
            raise RestrictionNotLiftable("safety_blocked")
        allowed = CAUSES[row.cause].lift_kinds if row.cause in CAUSES else frozenset()
        if not allowed:
            raise RestrictionNotLiftable("no_lift_condition")
        if lift_kind not in allowed:
            raise RestrictionNotLiftable("lift_kind_not_allowed")
        try:
            with transaction.atomic():
                lift = PlanRestrictionLift.objects.create(
                    restriction=row,
                    lift_kind=lift_kind,
                    answer_option_id=answer_option_id,
                    safety_state=safety.state,
                    safety_policy_version=safety.policy_version,
                    safety_evaluated_at_revision=safety.evaluated_at_revision,
                )
        except IntegrityError:  # гонка двух снятий: вторая читает строку первой
            return PlanRestrictionLift.objects.get(restriction=row), False
        return lift, True


__all__ = [
    "CAUSES",
    "LIFT_ANSWERED",
    "RestrictionMalformed",
    "RestrictionNotFound",
    "RestrictionNotLiftable",
    "RestrictionSpec",
    "blocking_question_ids",
    "create_restriction",
    "lift_restriction",
    "open_restriction",
    "open_restrictions",
    "parse_restriction",
    "parse_restrictions",
    "restrictions_document",
]
