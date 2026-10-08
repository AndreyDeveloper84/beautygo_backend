"""Plan Engine — шаг ↔ услуга ↔ запись (DRF-2868, WP2).

Контракт PLAN_ENGINE_CONTRACT v1.0 §4.3, §8.2, §8.3; уточнение 3 пакета
прохода 2 (исполнение при INCOMPLETE).

* ``resolve_step`` — переход шага вниз (``CAPABILITY → SERVICE → OFFER``) по
  решению резолвера. Каталог ранжирование не повторяет: он хранит, ЧЕМ
  разрешён шаг, и проверяет, что названные канон и предложение существуют и
  связаны между собой.
* ``link_booking`` — запись, сделанная от шага, как ФАКТ на шаге. Ничего не
  меняет ни в плане, ни в цели (§8.3): статус записи читается с ``Appointment``
  при чтении, поэтому отмена и перенос видны сами.
* ``step_admission`` — допуск шага к записи; проверяется сервером при каждой
  связи. Он же — вход для проверки ВНУТРИ транзакции создания записи, если
  запись получит блок происхождения (вопрос владельца В-1; до ответа связь
  ставится после факта отдельным вызовом).

Предложение шага — ``services.SalonService`` своего канона. Запись на
маркетплейсную ``services.Service`` шагом плана быть не может: у неё нет связи
с каноном, и доказать, что это предложение ИМЕННО этого шага, нечем.

``recommendation_id`` здесь не принимается ни от кого: сохранённой живой
``Recommendation`` не существует (C1), и поле остаётся пустым, а не
заполняется ``decision_id`` резолвера (§8.3 — «только при реальной цепочке»).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.db import IntegrityError, transaction

from appointments.models import Appointment
from goals.models import ClientGoal
from services.models import SalonService, ServiceTemplate

from .models import Plan, PlanRevision, PlanStepBooking, PlanStepResolution
from .plan_engine import PlanEngineDisabled, PlanEngineError, PlanNotFound, plan_engine_enabled
from .plan_restrictions import blocking_question_ids
from .plan_safety import SafetyInput, SafetyInputError, parse_safety_input

_LEVEL_ORDER = {"CAPABILITY": 0, "SERVICE": 1, "OFFER": 2}


class StepNotFound(PlanEngineError):
    """В текущей ревизии плана нет шага с таким ``step_id``."""


class StepNotExecutable(PlanEngineError):
    """Шаг не допущен к действию. ``reason`` — машинное имя условия;
    ``details`` — что вызывающему нужно показать человеку (идентификаторы
    открытых вопросов), без текста."""

    def __init__(self, reason: str, **details: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.details = details


class ResolutionRefused(PlanEngineError):
    """Переход уровня не принят. ``reason`` — машинное имя условия."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class AppointmentNotFound(PlanEngineError):
    """Нет такой записи у этого человека — чужая или отсутствует: «не найдено»."""


class BookingLinkConflict(PlanEngineError):
    """Запись уже связана с другим шагом: одна запись — один шаг."""


# ─── действующее состояние шага ──────────────────────────────────────────────


def _snapshot_step(revision: PlanRevision, step_id: str) -> dict | None:
    return next((s for s in revision.steps_snapshot if s.get("step_id") == step_id), None)


def effective_step(revision: PlanRevision, step_id: str) -> dict[str, Any] | None:
    """Шаг, как он есть сейчас: снимок ревизии + последний переход вниз.
    ``None`` — шага в ревизии нет."""
    step = _snapshot_step(revision, step_id)
    if step is None:
        return None
    state = {
        "level": step["level"],
        "canonical_service_ref": step.get("canonical_service_ref"),
        "tenant_offer_ref": step.get("tenant_offer_ref"),
        "resolver_decision_id": None,
        "recommendation_id": None,
    }
    resolutions = PlanStepResolution.objects.filter(plan_revision=revision, step_id=step_id)
    latest = max(resolutions, key=lambda r: _LEVEL_ORDER[r.level], default=None)
    if latest is not None and _LEVEL_ORDER[latest.level] > _LEVEL_ORDER[state["level"]]:
        state.update(
            level=latest.level,
            canonical_service_ref=str(latest.canonical_service_id),
            tenant_offer_ref=str(latest.tenant_offer_id) if latest.tenant_offer_id else None,
            resolver_decision_id=latest.resolver_decision_id,
            recommendation_id=str(latest.recommendation_id) if latest.recommendation_id else None,
        )
    return state


def _plan_gate(plan: Plan, revision: PlanRevision, step_id: str) -> None:
    """Общее для перехода уровня и для записи: план действует, правила не
    устарели, шаг есть и не заблокирован (уточнение 3 пакета)."""
    goal_active = plan.goal_id is not None and plan.goal.state == ClientGoal.State.ACTIVE
    if plan.status != Plan.Status.ACTIVE or not goal_active:
        raise StepNotExecutable("plan_not_in_effect")
    if revision.staleness != PlanRevision.Staleness.NONE:
        raise StepNotExecutable("revision_stale")
    if _snapshot_step(revision, step_id) is None:
        raise StepNotFound(step_id)
    # fail-closed: шаг без вердикта валидации не «допущен по умолчанию».
    verdict = (revision.validation.get("step_validations") or {}).get(step_id)
    if verdict == "BLOCKED":
        raise StepNotExecutable("step_blocked")
    if verdict not in ("VALID", "INCOMPLETE"):
        raise StepNotExecutable("step_validation_unknown")
    # DRF-2877 — стойкий незакрытый вопрос: открытое ограничение на всём плане
    # или на этом шаге. Последним: отказ называет вопрос только там, где шаг
    # иначе был бы исполним.
    questions = blocking_question_ids(plan, step_id)
    if questions:
        raise StepNotExecutable("restriction_open", question_ids=questions)


def step_admission(plan: Plan, revision: PlanRevision, step_id: str, *, salon_service_id: UUID | None) -> dict:
    """Допуск шага к ЗАПИСИ на конкретную услугу. Возвращает действующее
    состояние шага; отказ — ``StepNotExecutable`` / ``StepNotFound``.

    Запись допустима только с уровня OFFER и только на то предложение,
    которым разрешён шаг: явно запрошенное не подменяется (§8.3)."""
    _plan_gate(plan, revision, step_id)
    state = effective_step(revision, step_id)
    if state["level"] != "OFFER":
        raise StepNotExecutable("step_not_offer_level")
    if salon_service_id is None or str(salon_service_id) != state["tenant_offer_ref"]:
        raise StepNotExecutable("offer_mismatch")
    return state


def _safety_gate(safety: SafetyInput) -> None:
    """§6.1, §9.3: действие с шагом при STOP / UNKNOWN не выполняется. До
    транзакции и до любого чтения плана — отказ не зависит от того, чей план и
    есть ли он. Статус плана при этом не меняется: гейт отказывает действию."""
    if safety.blocks:
        raise StepNotExecutable("safety_blocked")
    # DRF-2877 — «уточнить» значит «сначала вопрос»: причина о человеке, область
    # — весь план. Отдельное имя, чтобы вызывающий задал вопрос, а не показал
    # запрет. Сам вопрос держится ограничением плана; это пол на случай, когда
    # вызывающий его не открыл.
    if safety.state == "CLARIFY":
        raise StepNotExecutable("clarify_pending")


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


# ─── переход шага вниз ───────────────────────────────────────────────────────


def resolve_step(
    user,
    plan_id: UUID,
    step_id: str,
    *,
    level: str,
    canonical_service_ref: UUID,
    tenant_offer_ref: UUID | None,
    resolver_decision_id: str,
    safety: SafetyInput,
) -> tuple[PlanStepResolution, bool]:
    """Записать, чем резолвер разрешил шаг. Возвращает ``(строка, created)``;
    повтор того же перехода с тем же содержимым — та же строка."""
    if not plan_engine_enabled():
        raise PlanEngineDisabled()
    if level not in PlanStepResolution.Level.values:
        raise ResolutionRefused("level_invalid")
    if (level == PlanStepResolution.Level.OFFER) != (tenant_offer_ref is not None):
        raise ResolutionRefused("level_refs_mismatch")
    if not isinstance(resolver_decision_id, str) or not resolver_decision_id.strip():
        # §8.2: шаг не получает услугу в обход резолвера.
        raise ResolutionRefused("resolver_decision_missing")
    _safety_gate(safety)

    with transaction.atomic():
        plan = _locked_plan(user, plan_id)
        revision = plan.current_revision
        _plan_gate(plan, revision, step_id)

        same = PlanStepResolution.objects.filter(plan_revision=revision, step_id=step_id, level=level).first()
        if same is not None:
            if (
                same.canonical_service_id == canonical_service_ref
                and same.tenant_offer_id == tenant_offer_ref
                and same.resolver_decision_id == resolver_decision_id
            ):
                return same, False
            raise ResolutionRefused("level_already_resolved")

        current = effective_step(revision, step_id)
        if _LEVEL_ORDER[level] <= _LEVEL_ORDER[current["level"]]:
            raise ResolutionRefused("not_downward")
        if current["canonical_service_ref"] not in (None, str(canonical_service_ref)):
            # Канон уже выбран на уровне SERVICE — предложение обязано быть его.
            raise ResolutionRefused("canonical_service_changed")

        if not ServiceTemplate.objects.filter(pk=canonical_service_ref).exists():
            raise ResolutionRefused("canonical_service_unknown")
        if tenant_offer_ref is not None:
            offer_template = (
                SalonService.objects.filter(pk=tenant_offer_ref).values_list("template_id", flat=True).first()
            )
            if offer_template is None:
                # Нет такого предложения — или оно вне канона (template NULL).
                raise ResolutionRefused("tenant_offer_unknown")
            if offer_template != canonical_service_ref:
                raise ResolutionRefused("offer_not_of_canonical_service")

        resolution = PlanStepResolution.objects.create(
            plan_revision=revision,
            step_id=step_id,
            level=level,
            canonical_service_id=canonical_service_ref,
            tenant_offer_id=tenant_offer_ref,
            resolver_decision_id=resolver_decision_id.strip(),
            safety_state=safety.state,
            safety_policy_version=safety.policy_version,
            safety_evaluated_at_revision=safety.evaluated_at_revision,
        )
    return resolution, True


# ─── запись как факт на шаге ─────────────────────────────────────────────────


def attach_booking(plan: Plan, step_id: str, appointment: Appointment, safety: SafetyInput) -> PlanStepBooking:
    """Связать запись с шагом. Вызывается ВНУТРИ открытой транзакции с уже
    запертым планом — и отсюда, и (если владелец разрешит В-1) из транзакции
    создания записи. Проверяет допуск шага; ни плана, ни цели не трогает."""
    _safety_gate(safety)
    revision = plan.current_revision
    state = step_admission(plan, revision, step_id, salon_service_id=appointment.salon_service_id)
    return PlanStepBooking.objects.create(
        plan=plan,
        plan_revision=revision,
        step_id=step_id,
        appointment=appointment,
        resolver_decision_id=state["resolver_decision_id"] or "",
        safety_state=safety.state,
        safety_policy_version=safety.policy_version,
        safety_evaluated_at_revision=safety.evaluated_at_revision,
    )


def link_booking(
    user, plan_id: UUID, step_id: str, appointment_id: UUID, *, safety: SafetyInput,
) -> tuple[PlanStepBooking, bool]:
    """Связь после факта: запись уже создана обычным путём. Возвращает
    ``(строка, created)``; повтор — та же строка."""
    if not plan_engine_enabled():
        raise PlanEngineDisabled()
    _safety_gate(safety)
    try:
        with transaction.atomic():
            plan = _locked_plan(user, plan_id)
            appointment = Appointment.objects.filter(pk=appointment_id, client=user).first()
            if appointment is None:
                raise AppointmentNotFound(str(appointment_id))
            existing = PlanStepBooking.objects.filter(appointment=appointment).first()
            if existing is not None:
                if existing.plan_id == plan.id and existing.step_id == step_id:
                    return existing, False
                raise BookingLinkConflict()
            # Запись, сделанная до сохранения плана, не могла быть сделана от
            # его шага — задним числом её к шагу не приписывают.
            if appointment.created_at < plan.created_at:
                raise StepNotExecutable("booking_predates_plan")
            link = attach_booking(plan, step_id, appointment, safety)
    except IntegrityError as exc:  # гонка двух связей одной записи — OneToOne
        raise BookingLinkConflict() from exc
    return link, True


# ─── запись от шага: происхождение в создании записи (решение владельца 07.10) ──

ENTRY_POINT_PLAN_STEP = "PLAN_STEP"


class ProvenanceMalformed(PlanEngineError):
    """Блок происхождения не конформен. ``reason`` — машинное имя."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PlanStepProvenance:
    """Откуда запись: шаг какого плана и при какой безопасности хода (канон
    §16.4: ``entry_point = PLAN_STEP`` + ссылки на план и шаг)."""

    plan_id: UUID
    step_id: str
    safety: SafetyInput


def parse_booking_provenance(raw: Any) -> PlanStepProvenance | None:
    """Необязательный блок ``provenance`` тела создания записи. ``None`` —
    блока нет: обычная запись, плана она не касается."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ProvenanceMalformed("provenance_not_object")
    if raw.get("entry_point") != ENTRY_POINT_PLAN_STEP:
        raise ProvenanceMalformed("entry_point_unsupported")
    try:
        plan_id = UUID(str(raw.get("plan_id")))
    except (TypeError, ValueError) as exc:
        raise ProvenanceMalformed("plan_id_malformed") from exc
    step_id = raw.get("step_id")
    if not isinstance(step_id, str) or not step_id.strip():
        raise ProvenanceMalformed("step_id_missing")
    try:
        safety = parse_safety_input(raw)
    except SafetyInputError as exc:
        raise ProvenanceMalformed(exc.reason) from exc
    return PlanStepProvenance(plan_id=plan_id, step_id=step_id, safety=safety)


def admit_step_for_booking(client_id: UUID, provenance: PlanStepProvenance, *, salon_service_id: UUID | None) -> Plan:
    """Допуск шага ДО создания записи, внутри её транзакции.

    Идентификаторы из тела допуска не обходят: план ищется строго среди планов
    этого человека, шаг — в его текущей ревизии, услуга — та, которой разрешён
    шаг. Возвращает запертый план для ``attach_booking``. Отказ — исключение;
    транзакция записи откатывается, записи нет.
    """
    if not plan_engine_enabled():
        raise PlanEngineDisabled()
    _safety_gate(provenance.safety)
    plan = _locked_plan(client_id, provenance.plan_id)
    step_admission(plan, plan.current_revision, provenance.step_id, salon_service_id=salon_service_id)
    return plan


# ─── чтение ──────────────────────────────────────────────────────────────────


def step_state(plan: Plan, revision: PlanRevision) -> dict[str, Any]:
    """По каждому шагу ревизии: действующий уровень и записи, сделанные от
    шага. Статус и время записи — с ``Appointment`` на момент чтения. Ни
    счётчика, ни признака «выполнено» (PE-4)."""
    bookings: dict[str, list[dict]] = {}
    for link in (
        PlanStepBooking.objects.filter(plan=plan, plan_revision=revision)
        .select_related("appointment")
        .order_by("created_at")
    ):
        bookings.setdefault(link.step_id, []).append(
            {
                "appointment_id": str(link.appointment_id),
                "status": link.appointment.status,
                "start_datetime": link.appointment.start_datetime.isoformat(),
            }
        )
    out: dict[str, Any] = {}
    for step in revision.steps_snapshot:
        step_id = step["step_id"]
        out[step_id] = {
            **effective_step(revision, step_id),
            "bookings": bookings.get(step_id, []),
            # DRF-2877 — на шаге или на всём плане есть незакрытый вопрос.
            "restricted": bool(blocking_question_ids(plan, step_id)),
        }
    return out
