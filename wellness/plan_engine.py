"""Plan Engine — граница сохранения durable-плана (DRF-2857, WP1).

Контракт: PLAN_ENGINE_CONTRACT v1.0 §4.4–§4.9. Здесь — только ХРАНЕНИЕ:

* ``create_plan_from_command`` — идемпотентная команда сохранения (§4.9):
  одна транзакция «``Plan`` + первая ``PlanRevision`` + прежний ``active``
  план той же цели → ``superseded``»;
* ``set_plan_status`` — пауза / возобновление / архив по слову человека
  (§4.5); автоматических писателей статуса нет;
* ``plan_payload`` — чтение плана действующей цели.

Композицию и семантическую валидацию делает ayla-ai-core (§10.1): каталог
решение не пересчитывает и не «чинит» — хранит то, что человек видел, и
проверяет только ФОРМУ (§4.2, §4.8). ``PlanDecision`` эфемерен (§4.1), поэтому
команда несёт снимок шагов с собой.

Только SAVE (§10.2): FOLLOW в объём первого сценария не входит, команда с
другим ``mode`` отвергается, а не сохраняется как SAVE молча.

Флаг ``PLAN_ENGINE_ENABLED`` (default false). Включён — писатель Lite
(``wellness.plan_lite.create_plan``) отказывает, а действующий Lite-план
человека закрывается здесь как ``superseded`` той же транзакцией, что создаёт
durable-план: в любой момент у человека действует один механизм. Выключили —
Lite снова пишет; строки ``Plan``/``PlanRevision`` остаются.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from goals.models import ClientGoal

from .models import (
    PLAN_FORBIDDEN_FIELDS,
    PLAN_POLICY_VERSION_KEYS,
    PersonalPlan,
    Plan,
    PlanRevision,
)

#: Ключи шага — закрытый набор §4.2. Незнакомый ключ — отказ: у шага нет
#: места для произвольного текста (PE-2), и появиться ему негде.
STEP_KEYS: frozenset[str] = frozenset(
    {
        "step_id", "role", "outcome_ref", "capability_ref", "level",
        "canonical_service_ref", "tenant_offer_ref", "assertions", "execution",
        "alternative_of", "provenance",
    }
)
STEP_ROLES: frozenset[str] = frozenset({"CORE", "OPTIONAL", "ALTERNATIVE"})
STEP_LEVELS: frozenset[str] = frozenset({"CAPABILITY", "SERVICE", "OFFER"})
VALIDATION_STATUSES: frozenset[str] = frozenset({"VALID", "INCOMPLETE", "BLOCKED"})
MODE_SAVE = "SAVE"


def plan_engine_enabled() -> bool:
    return bool(getattr(settings, "PLAN_ENGINE_ENABLED", False))


class PlanEngineError(Exception):
    """Base for Plan Engine refusals."""


class PlanEngineDisabled(PlanEngineError):
    """Флаг выключен — штатный отказ, не сбой."""


class ContractViolation(PlanEngineError):
    """Команда не конформна контракту — отвергается ЦЕЛИКОМ (§4.1), частичного
    сохранения нет. ``reason`` — машинное имя нарушенного правила."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class GoalRequired(PlanEngineError):
    """Команда без цели. Допустим ли durable-план без цели — вопрос владельца
    Q-PE-2; до ответа писатель отказывает, а не решает за него."""


class GoalNotFound(PlanEngineError):
    """Нет такой активной цели у этого человека — чужая или закрытая:
    «не найдено», не «чужое»."""


class IdempotencyConflict(PlanEngineError):
    """Тот же ключ команды, другое содержимое — клиент переиспользовал
    подтверждение; ничего не записано."""


class PlanNotFound(PlanEngineError):
    """Нет такого плана у этого человека."""


class TransitionRefused(PlanEngineError):
    """Переход статуса не разрешён (§4.5)."""

    def __init__(self, from_status: str, to_status: str) -> None:
        super().__init__(f"{from_status} -> {to_status}")
        self.from_status = from_status
        self.to_status = to_status


class ActivePlanExists(PlanEngineError):
    """Возобновить нельзя: у цели уже действует другой план (§4.7)."""


# ─── разбор команды ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PlanCommand:
    decision_id: UUID
    goal_ref: UUID
    question_id: str
    option_id: str
    state_revision: int
    steps: list[dict]
    assertions: list[dict]
    validation: dict
    policy_versions: dict


def _forbidden_key(node: Any) -> str | None:
    """Первый ключ из закрытого списка §4.8 на любой глубине, иначе None."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in PLAN_FORBIDDEN_FIELDS:
                return key
            found = _forbidden_key(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _forbidden_key(item)
            if found:
                return found
    return None


def _ref(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _uuid(raw: Any, name: str) -> UUID:
    try:
        return UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise ContractViolation(f"{name}_malformed") from exc


def _validate_steps(steps: Any, assertion_ids: set[str]) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        # §6.2: пустой набор шагов — честный «плана нет», сохранять нечего.
        raise ContractViolation("steps_empty")
    seen: set[str] = set()
    for step in steps:
        if not isinstance(step, dict):
            raise ContractViolation("step_not_object")
        unknown = set(step) - STEP_KEYS
        if unknown:
            forbidden = sorted(unknown & PLAN_FORBIDDEN_FIELDS)
            if forbidden:
                raise ContractViolation("forbidden_field", forbidden[0])
            raise ContractViolation("step_unknown_key", sorted(unknown)[0])
        nested = _forbidden_key(step)
        if nested:
            raise ContractViolation("forbidden_field", nested)
        step_id = step.get("step_id")
        if not _ref(step_id):
            raise ContractViolation("step_id_missing")
        if step_id in seen:
            raise ContractViolation("step_id_duplicate", step_id)
        seen.add(step_id)
        if step.get("role") not in STEP_ROLES:
            raise ContractViolation("step_role_invalid", step_id)
        if not _ref(step.get("capability_ref")):
            # PE-1 / PE-6: вход шага — способность; шаг без неё не существует.
            raise ContractViolation("capability_ref_missing", step_id)
        level = step.get("level")
        if level not in STEP_LEVELS:
            raise ContractViolation("step_level_invalid", step_id)
        service = step.get("canonical_service_ref")
        offer = step.get("tenant_offer_ref")
        expected = {
            "CAPABILITY": (False, False),
            "SERVICE": (True, False),
            "OFFER": (True, True),
        }[level]
        if (service is not None, offer is not None) != expected:
            raise ContractViolation("step_level_refs_mismatch", step_id)
        refs = step.get("assertions", [])
        if not isinstance(refs, list) or not all(_ref(r) for r in refs):
            raise ContractViolation("step_assertions_malformed", step_id)
        missing = [r for r in refs if r not in assertion_ids]
        if missing:
            # PE-5: утверждение без снимка не воспроизводимо — не попадает.
            raise ContractViolation("assertion_not_in_snapshot", missing[0])
    for step in steps:
        alt = step.get("alternative_of")
        if step["role"] == "ALTERNATIVE":
            if alt not in seen or alt == step["step_id"]:
                raise ContractViolation("alternative_of_invalid", step["step_id"])
        elif alt is not None:
            raise ContractViolation("alternative_of_on_non_alternative", step["step_id"])
    return steps


def parse_command(raw: Any) -> PlanCommand:
    """Форма ``PlanCreateCommand`` §4.9 + снимок решения. Всё неконформное —
    отказ целиком, не молчаливая правка."""
    if not isinstance(raw, dict):
        raise ContractViolation("command_not_object")
    if raw.get("mode", MODE_SAVE) != MODE_SAVE:
        raise ContractViolation("mode_not_supported", str(raw.get("mode")))
    decision_id = _uuid(raw.get("decision_id"), "decision_id")
    if raw.get("goal_ref") in (None, ""):
        raise GoalRequired()
    goal_ref = _uuid(raw.get("goal_ref"), "goal_ref")

    confirmation = raw.get("confirmation")
    if not isinstance(confirmation, dict):
        raise ContractViolation("confirmation_missing")
    question_id = confirmation.get("question_id")
    option_id = confirmation.get("option_id")
    state_revision = confirmation.get("state_revision")
    if not _ref(question_id) or not _ref(option_id):
        raise ContractViolation("confirmation_incomplete")
    if isinstance(state_revision, bool) or not isinstance(state_revision, int) or state_revision < 0:
        raise ContractViolation("confirmation_state_revision_malformed")

    provenance = raw.get("provenance")
    policy_versions = provenance.get("policy_versions") if isinstance(provenance, dict) else None
    if not isinstance(policy_versions, dict) or set(policy_versions) != PLAN_POLICY_VERSION_KEYS:
        raise ContractViolation("policy_versions_malformed")
    if not all(_ref(v) for v in policy_versions.values()):
        raise ContractViolation("policy_versions_malformed")

    decision = raw.get("decision")
    if not isinstance(decision, dict):
        raise ContractViolation("decision_missing")
    assertions = decision.get("assertions", [])
    if not isinstance(assertions, list) or not all(
        isinstance(a, dict) and _ref(a.get("assertion_id")) for a in assertions
    ):
        raise ContractViolation("assertions_malformed")
    validation = decision.get("validation")
    if not isinstance(validation, dict) or validation.get("status") not in VALIDATION_STATUSES:
        raise ContractViolation("validation_malformed")
    nested = _forbidden_key(validation)
    if nested:
        raise ContractViolation("forbidden_field", nested)
    steps = _validate_steps(decision.get("steps"), {a["assertion_id"] for a in assertions})

    return PlanCommand(
        decision_id=decision_id,
        goal_ref=goal_ref,
        question_id=question_id,
        option_id=option_id,
        state_revision=state_revision,
        steps=steps,
        assertions=assertions,
        validation=validation,
        policy_versions=policy_versions,
    )


def idempotency_key(user_id: Any, command: PlanCommand) -> str:
    """§4.9: ``hash(subject_user_id, decision_id, confirmation_id)``.

    Отдельного ``confirmation_id`` в схеме подтверждения §4.9 нет, и в слитом
    коде бота его тоже нет — подтверждение опознаётся тройкой
    ``question_id + option_id + state_revision``. Считает сервер: ключ от
    клиента не принимается."""
    material = "|".join(
        (
            str(user_id),
            str(command.decision_id),
            command.question_id,
            command.option_id,
            str(command.state_revision),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def content_hash(command: PlanCommand) -> str:
    canonical = json.dumps(
        {
            "goal_ref": str(command.goal_ref),
            "steps": command.steps,
            "assertions": command.assertions,
            "validation": command.validation,
            "policy_versions": command.policy_versions,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ─── писатель ────────────────────────────────────────────────────────────────


def _replay(key: str, digest: str) -> Plan | None:
    """Повтор команды → тот же план (§4.9); тот же ключ с другим содержимым —
    ``IdempotencyConflict``. Сверяется первая ревизия: её создала команда."""
    plan = Plan.objects.filter(idempotency_key=key).first()
    if plan is None:
        return None
    first = PlanRevision.objects.filter(plan=plan, revision_no=1).values_list("content_hash", flat=True).first()
    if first != digest:
        raise IdempotencyConflict()
    return plan


def create_plan_from_command(user, command: PlanCommand) -> tuple[Plan, bool]:
    """Сохранить план по подтверждению человека. Возвращает ``(plan, created)``;
    повтор команды — тот же ``Plan`` и ``created=False``, без второй ревизии."""
    if not plan_engine_enabled():
        raise PlanEngineDisabled()
    key = idempotency_key(user.pk, command)
    digest = content_hash(command)
    existing = _replay(key, digest)
    if existing is not None:
        return existing, False

    try:
        with transaction.atomic():
            # Замок на строке цели: две команды на одну цель идут по очереди,
            # и вторая видит план, созданный первой.
            goal = (
                ClientGoal.objects.select_for_update()
                .filter(pk=command.goal_ref, client=user, state=ClientGoal.State.ACTIVE)
                .first()
            )
            if goal is None:
                raise GoalNotFound(str(command.goal_ref))
            existing = _replay(key, digest)
            if existing is not None:
                return existing, False

            now = timezone.now()
            Plan.objects.filter(goal=goal, status=Plan.Status.ACTIVE).update(
                status=Plan.Status.SUPERSEDED, status_changed_at=now,
            )
            # Один механизм на человека: действующий Lite-план закрывается
            # этим же сохранением. Строка и её обязательства остаются историей.
            PersonalPlan.objects.filter(user=user, status=PersonalPlan.Status.ACTIVE).update(
                status=PersonalPlan.Status.SUPERSEDED, closed_at=now,
            )
            plan = Plan.objects.create(
                subject_user=user, goal=goal, idempotency_key=key, status_changed_at=now,
            )
            revision = PlanRevision.objects.create(
                plan=plan,
                revision_no=1,
                steps_snapshot=command.steps,
                assertions_snapshot=command.assertions,
                validation=command.validation,
                policy_versions=command.policy_versions,
                created_from={"decision_id": str(command.decision_id)},
                content_hash=digest,
            )
            plan.current_revision = revision
            plan.save(update_fields=["current_revision"])
    except IntegrityError:
        # Гонка той же команды мимо замка (цель сменилась между чтениями):
        # победитель уже записал план под этим ключом.
        existing = _replay(key, digest)
        if existing is None:
            raise
        return existing, False
    return plan, True


#: Переходы по слову человека (§4.5). ``superseded`` и ``archived`` терминальны.
_ALLOWED: dict[str, frozenset[str]] = {
    Plan.Status.ACTIVE: frozenset({Plan.Status.PAUSED, Plan.Status.ARCHIVED}),
    Plan.Status.PAUSED: frozenset({Plan.Status.ACTIVE, Plan.Status.ARCHIVED}),
    Plan.Status.SUPERSEDED: frozenset(),
    Plan.Status.ARCHIVED: frozenset(),
}
#: Что человек вправе запросить сам; ``superseded`` пишет только сохранение
#: нового плана.
REQUESTABLE_STATUSES: frozenset[str] = frozenset(
    {Plan.Status.ACTIVE, Plan.Status.PAUSED, Plan.Status.ARCHIVED}
)


def set_plan_status(user, plan_id: UUID, to_status: str) -> Plan:
    """Пауза / возобновление / архив. Запрос текущего статуса — не ошибка и не
    запись (повтор кнопки)."""
    if not plan_engine_enabled():
        raise PlanEngineDisabled()
    try:
        with transaction.atomic():
            plan = Plan.objects.select_for_update().filter(pk=plan_id, subject_user=user).first()
            if plan is None:
                raise PlanNotFound(str(plan_id))
            if to_status == plan.status and to_status in REQUESTABLE_STATUSES:
                return plan
            if to_status not in _ALLOWED[plan.status]:
                raise TransitionRefused(plan.status, to_status)
            plan.status = to_status
            plan.status_changed_at = timezone.now()
            plan.save(update_fields=["status", "status_changed_at"])
    except IntegrityError as exc:  # plan_one_active_per_goal
        raise ActivePlanExists() from exc
    return plan


# ─── чтение ──────────────────────────────────────────────────────────────────


def plan_document(plan: Plan) -> dict[str, Any]:
    """Документ плана. Только то, что сохранено: ни прогресса, ни счётчиков
    (PE-4) — их нет в модели и не появляется здесь."""
    revision = plan.current_revision
    goal_state = plan.goal.state if plan.goal_id else None
    return {
        "plan_id": str(plan.id),
        "status": plan.status,
        "goal_id": str(plan.goal_id) if plan.goal_id else None,
        "goal_state": goal_state,
        # «План действует» = план active И цель active. Статус цели в статус
        # плана не переписывается (§4.5 — отложено решением 07.10).
        "in_effect": plan.status == Plan.Status.ACTIVE and goal_state == ClientGoal.State.ACTIVE,
        "created_via": plan.created_via,
        "created_at": plan.created_at.isoformat(),
        "status_changed_at": plan.status_changed_at.isoformat(),
        "revision": {
            "plan_revision_id": str(revision.id),
            "revision_no": revision.revision_no,
            "steps": revision.steps_snapshot,
            "assertions": revision.assertions_snapshot,
            "validation": revision.validation,
            "staleness": revision.staleness,
            "policy_versions": revision.policy_versions,
            "created_from": revision.created_from,
            "created_at": revision.created_at.isoformat(),
        },
    }


def plan_payload(user) -> dict[str, Any] | None:
    """План действующей цели человека; ``None`` — плана нет или флаг выключен.

    ``active`` раньше ``paused``: сохранение нового плана гасит только
    прежний ``active`` (§4.9), поэтому у цели могут быть и действующий, и
    приостановленный — отдаётся действующий, иначе последний приостановленный."""
    if not plan_engine_enabled():
        return None
    plans = Plan.objects.filter(
        subject_user=user, goal__state=ClientGoal.State.ACTIVE,
    ).select_related("goal", "current_revision")
    plan = (
        plans.filter(status=Plan.Status.ACTIVE).first()
        or plans.filter(status=Plan.Status.PAUSED).order_by("-status_changed_at").first()
    )
    return plan_document(plan) if plan is not None else None
