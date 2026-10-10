"""Internal bot-service API — durable Plan (DRF-2857, WP1).

``/api/v1/internal/me/plan/`` — тот же auth-паттерн, что у Plan Lite и
wellness-context: Bearer service token + X-External-User-ID, разрешённый в
``request.user`` через ``IsBotServiceWithVerifiedClient``.

- ``POST`` — команда сохранения (контракт §4.9): ``{decision_id, goal_ref,
  mode?: "SAVE", confirmation: {question_id, option_id, state_revision},
  provenance: {policy_versions: {…6}}, decision: {steps[], assertions[],
  validation}}``. 201 — создан; 200 — повтор той же команды (тот же план,
  второй ревизии нет); 400 ``PLAN_CONTRACT_VIOLATION`` c ``details.reason`` —
  форма не конформна, ничего не записано; 404 ``NOT_FOUND`` — цель не у этого
  человека / не активна; 409 ``PLAN_IDEMPOTENCY_CONFLICT`` — то же
  подтверждение с другим содержимым.
- ``GET`` — план действующей цели: ``{plan: {...} | null}``. Флаг выключен —
  ``{plan: null}``, не ошибка.
- ``POST state/`` — ``{plan_id, state: active | paused | archived}``: пауза,
  возобновление, архив по слову человека (§4.5). 409
  ``PLAN_TRANSITION_REFUSED`` — переход не разрешён; 409 ``PLAN_ACTIVE_EXISTS``
  — у цели уже действует другой план.
- Флаг выключен → писатели отвечают 404 ``PLAN_ENGINE_DISABLED`` по замыслу,
  не 5xx (общий breaker бота считает постоянный 5xx аварией).
"""
from __future__ import annotations

from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from services.capabilities import capability_labels
from services.synthetic import grant_for
from users.permissions import IsBotServiceWithVerifiedClient
from users.response import error_response, success_response

from .plan_gate import archiving_own_plan, gated
from .plan_engine import (
    REQUESTABLE_STATUSES,
    ActivePlanExists,
    CapabilityNotConfirmed,
    ContractViolation,
    GoalNotFound,
    GoalRequired,
    IdempotencyConflict,
    PlanEngineDisabled,
    PlanNotFound,
    ReplacementTargetChanged,
    SaveSafetyBlocked,
    TransitionRefused,
    create_plan_from_command,
    parse_command,
    plan_document,
    plan_engine_enabled,
    plan_payload,
    proposal_payload,
    replace_plan,
    replaced_by,
    set_plan_status,
)
from .plan_compose import compose_plan, parse_compose_request
from .plan_restrictions import (
    RestrictionMalformed,
    RestrictionNotFound,
    RestrictionNotLiftable,
    lift_restriction,
    open_restriction,
    parse_restriction,
)
from .plan_safety import SafetyInputError, parse_safety_input, parse_step_safety_input
from .plan_engine_steps import (
    AppointmentNotFound,
    BookingLinkConflict,
    ProvenanceMalformed,
    ResolutionRefused,
    StepNotExecutable,
    StepNotFound,
    candidates_for_step,
    link_booking,
    resolve_step,
)


def _disabled() -> Response:
    return error_response(
        "PLAN_ENGINE_DISABLED",
        "Plan Engine выключен (PLAN_ENGINE_ENABLED)",
        status_code=status.HTTP_404_NOT_FOUND,
    )


class PlanEngineView(APIView):
    """GET / POST /api/v1/internal/me/plan/"""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={200: OpenApiResponse(description="{plan: document | null}")},
    )
    def get(self, request: Request) -> Response:
        # DRF-2857 — действующий план и, отдельно, предложение, которое ждёт
        # подтверждения замены. Одно другое не заслоняет.
        return success_response({"plan": plan_payload(request.user), "proposal": proposal_payload(request.user)})

    @extend_schema(
        tags=["internal"],
        responses={
            201: OpenApiResponse(description="Plan saved; body = {plan, created: true}"),
            200: OpenApiResponse(description="Replay of the same command; body = {plan, created: false}"),
            400: OpenApiResponse(description="PLAN_CONTRACT_VIOLATION, details.reason"),
            404: OpenApiResponse(description="Goal not found for the caller, or PLAN_ENGINE_DISABLED"),
            409: OpenApiResponse(description="PLAN_IDEMPOTENCY_CONFLICT"),
        },
    )
    @gated
    def post(self, request: Request) -> Response:
        try:
            command = parse_command(request.data)
            plan, created = create_plan_from_command(request.user, command)
        except PlanEngineDisabled:
            return _disabled()
        except GoalRequired:
            return error_response(
                "PLAN_CONTRACT_VIOLATION",
                "План сохраняется только к цели",
                details={"reason": "goal_required"},
            )
        except ContractViolation as exc:
            return error_response(
                "PLAN_CONTRACT_VIOLATION",
                "Команда сохранения плана не соответствует контракту",
                details={"reason": exc.reason, "detail": exc.detail},
            )
        except GoalNotFound:
            return error_response(
                "NOT_FOUND",
                "Активная цель не найдена",
                details={"reason": "goal_not_found"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except SaveSafetyBlocked:
            return error_response(
                "PLAN_SAVE_SAFETY_BLOCKED",
                "План сейчас не сохраняется",
                status_code=status.HTTP_409_CONFLICT,
            )
        except CapabilityNotConfirmed as exc:
            return error_response(
                "PLAN_CAPABILITY_NOT_CONFIRMED",
                "План опирается на знание, которое не подтверждено, — сохранить его нельзя",
                details={"capability_ref": exc.capability_ref},
                status_code=status.HTTP_409_CONFLICT,
            )
        except IdempotencyConflict:
            return error_response(
                "PLAN_IDEMPOTENCY_CONFLICT",
                "Это подтверждение уже использовано для другого плана",
                status_code=status.HTTP_409_CONFLICT,
            )
        plan.refresh_from_db()
        return success_response(
            {**_saved(plan), "created": created},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


def _saved(plan) -> dict:
    """Ответ сохранения: документ плана и — только у предложения — какой
    действующий план оно заменит. ``status == "proposed"`` ⇔ ``replaces`` есть."""
    body = {"plan": plan_document(plan)}
    replaces = replaced_by(plan)
    if replaces is not None:
        body["replaces"] = {"plan_id": str(replaces)}
    return body


class PlanReplaceView(APIView):
    """POST /api/v1/internal/me/plan/replace/ — подтверждённая замена
    действующего плана предложением (DRF-2857).

    Тело: ``{plan_id, replaces_plan_id, safety_state, safety_policy_version,
    evaluated_at_revision}``. ``replaces_plan_id`` — план, о замене которого
    человек сказал «да»: подтверждение относится к нему, а не к любому.
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            200: OpenApiResponse(description="Replaced (or already replaced); body = {plan, replaced: bool}"),
            400: OpenApiResponse(description="PLAN_CONTRACT_VIOLATION, details.reason"),
            404: OpenApiResponse(description="Plan not found for the caller, or PLAN_ENGINE_DISABLED"),
            409: OpenApiResponse(
                description="PLAN_REPLACEMENT_TARGET_CHANGED | PLAN_TRANSITION_REFUSED | PLAN_SAVE_SAFETY_BLOCKED",
            ),
        },
    )
    @gated
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        try:
            plan_id = _uuid_field(data, "plan_id")
            replaces_plan_id = _uuid_field(data, "replaces_plan_id")
            safety = parse_safety_input(data)
        except SafetyInputError as exc:  # раньше ValueError: SafetyInputError — его род
            return error_response("PLAN_CONTRACT_VIOLATION", "Запрос не конформен", details={"reason": exc.reason})
        except ValueError as exc:
            return error_response(
                "PLAN_CONTRACT_VIOLATION", "Запрос не конформен", details={"reason": f"{exc}_malformed"},
            )
        try:
            plan, replaced = replace_plan(request.user, plan_id, replaces_plan_id, safety)
        except PlanEngineDisabled:
            return _disabled()
        except PlanNotFound:
            return error_response(
                "NOT_FOUND", "План не найден",
                details={"reason": "plan_not_found"}, status_code=status.HTTP_404_NOT_FOUND,
            )
        except SaveSafetyBlocked:
            return error_response(
                "PLAN_SAVE_SAFETY_BLOCKED", "План сейчас не заменяется", status_code=status.HTTP_409_CONFLICT,
            )
        except ReplacementTargetChanged as exc:
            return error_response(
                "PLAN_REPLACEMENT_TARGET_CHANGED",
                "Действующий план изменился — подтверждение относилось к другому",
                details={"current_plan_id": str(exc.current_plan_id) if exc.current_plan_id else None},
                status_code=status.HTTP_409_CONFLICT,
            )
        except TransitionRefused as exc:
            return error_response(
                "PLAN_TRANSITION_REFUSED",
                "Этот план нельзя перевести в запрошенное состояние",
                details={"from": exc.from_status, "to": exc.to_status},
                status_code=status.HTTP_409_CONFLICT,
            )
        plan.refresh_from_db()
        return success_response({"plan": plan_document(plan), "replaced": replaced})


class PlanEngineStateView(APIView):
    """POST /api/v1/internal/me/plan/state/"""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            200: OpenApiResponse(description="Status set; body = {plan}"),
            400: OpenApiResponse(description="Malformed plan_id / state"),
            404: OpenApiResponse(description="Plan not found for the caller, or PLAN_ENGINE_DISABLED"),
            409: OpenApiResponse(description="PLAN_TRANSITION_REFUSED | PLAN_ACTIVE_EXISTS"),
        },
    )
    @gated(unless=archiving_own_plan)
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        try:
            plan_id = UUID(str(data.get("plan_id")))
        except (TypeError, ValueError):
            return error_response("VALIDATION_ERROR", "plan_id must be a UUID")
        to_status = data.get("state")
        if to_status not in REQUESTABLE_STATUSES:
            return error_response(
                "VALIDATION_ERROR", f"state must be one of {sorted(REQUESTABLE_STATUSES)}",
            )
        try:
            plan = set_plan_status(request.user, plan_id, to_status)
        except PlanEngineDisabled:
            return _disabled()
        except PlanNotFound:
            return error_response(
                "NOT_FOUND", "План не найден", status_code=status.HTTP_404_NOT_FOUND,
            )
        except TransitionRefused as exc:
            return error_response(
                "PLAN_TRANSITION_REFUSED",
                "Этот план нельзя перевести в запрошенное состояние",
                details={"from": exc.from_status, "to": exc.to_status},
                status_code=status.HTTP_409_CONFLICT,
            )
        except ActivePlanExists:
            return error_response(
                "PLAN_ACTIVE_EXISTS",
                "У цели уже действует другой план",
                status_code=status.HTTP_409_CONFLICT,
            )
        plan.refresh_from_db()
        return success_response({"plan": plan_document(plan)})


def _uuid_field(data: dict, name: str, *, required: bool = True):
    raw = data.get(name)
    if raw in (None, ""):
        if required:
            raise ValueError(name)
        return None
    try:
        return UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(name) from exc


def _step_refusal(exc: Exception) -> Response:
    """Общие отказы двух ручек шага — одним местом, чтобы коды не разошлись."""
    if isinstance(exc, PlanEngineDisabled):
        return _disabled()
    if isinstance(exc, PlanNotFound):
        return error_response(
            "NOT_FOUND", "План не найден",
            details={"reason": "plan_not_found"}, status_code=status.HTTP_404_NOT_FOUND,
        )
    if isinstance(exc, StepNotFound):
        return error_response(
            "NOT_FOUND", "Шаг не найден в текущей ревизии плана",
            details={"reason": "step_not_found"}, status_code=status.HTTP_404_NOT_FOUND,
        )
    if isinstance(exc, StepNotExecutable):
        return error_response(
            "PLAN_STEP_NOT_EXECUTABLE",
            "Шаг плана сейчас не допущен к действию",
            details={"reason": exc.reason, **exc.details},
            status_code=status.HTTP_409_CONFLICT,
        )
    raise exc


def plan_step_refusal_response(exc: Exception) -> Response:
    """Отказы допуска шага для создания записи с блоком происхождения
    (``appointments.internal_api``): те же коды, что у ручек шага."""
    if isinstance(exc, ProvenanceMalformed):
        return error_response(
            "PLAN_CONTRACT_VIOLATION",
            "Блок происхождения записи не соответствует контракту",
            details={"reason": exc.reason, "detail": ""},
        )
    if isinstance(exc, BookingLinkConflict):
        return error_response(
            "PLAN_STEP_BOOKING_CONFLICT",
            "Эта запись уже связана с другим шагом",
            status_code=status.HTTP_409_CONFLICT,
        )
    return _step_refusal(exc)


class PlanStepResolutionView(APIView):
    """POST /api/v1/internal/me/plan/steps/resolution/

    ``{plan_id, step_id, level: SERVICE | OFFER, canonical_service_ref,
    tenant_offer_ref?, resolver_decision_id, safety_state,
    safety_policy_version, evaluated_at_revision}`` — чем резолвер разрешил шаг
    (контракт §4.3, §8.2). 201 — записано; 200 — повтор того же перехода; 409
    ``PLAN_STEP_RESOLUTION_REFUSED`` / ``PLAN_STEP_NOT_EXECUTABLE`` с
    ``details.reason``. ``recommendation_id`` не принимается (§8.3).
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(tags=["internal"], responses={201: OpenApiResponse(description="{plan, created}")})
    @gated
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        try:
            plan_id = _uuid_field(data, "plan_id")
            canonical = _uuid_field(data, "canonical_service_ref")
            offer = _uuid_field(data, "tenant_offer_ref", required=False)
        except ValueError as exc:
            return error_response("VALIDATION_ERROR", f"{exc} must be a UUID")
        step_id = data.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            return error_response("VALIDATION_ERROR", "step_id is required")
        try:
            safety = parse_step_safety_input(data)
        except SafetyInputError as exc:
            return error_response(
                "PLAN_CONTRACT_VIOLATION",
                "Действие с шагом плана требует состояния безопасности хода",
                details={"reason": exc.reason, "detail": exc.detail},
            )
        try:
            resolution, created = resolve_step(
                request.user,
                plan_id,
                step_id,
                level=data.get("level"),
                canonical_service_ref=canonical,
                tenant_offer_ref=offer,
                resolver_decision_id=data.get("resolver_decision_id"),
                safety=safety,
            )
        except ResolutionRefused as exc:
            return error_response(
                "PLAN_STEP_RESOLUTION_REFUSED",
                "Переход шага не принят",
                details={"reason": exc.reason},
                status_code=status.HTTP_409_CONFLICT,
            )
        except (PlanEngineDisabled, PlanNotFound, StepNotFound, StepNotExecutable) as exc:
            return _step_refusal(exc)
        plan = resolution.plan_revision.plan
        return success_response(
            {"plan": plan_document(plan), "created": created},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PlanStepCandidatesView(APIView):
    """POST /api/v1/internal/me/plan/steps/candidates/ — кандидаты услуги для
    шага (DRF-2868, контракт §8.2). Ничего не пишет.

    Тело: ``{plan_id, step_id, safety_state, safety_policy_version,
    evaluated_at_revision}``. Каталог сам ищет предложения по способности шага
    в видимости этого человека, проверяет допуск и происхождение ответа о
    проверке здоровья. Порядок кандидатов — не ранжирование.
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            200: OpenApiResponse(
                description="{step_id, capability_ref, candidates[], search_id, nothing_because, rejected}",
            ),
            400: OpenApiResponse(description="PLAN_CONTRACT_VIOLATION, details.reason"),
            404: OpenApiResponse(description="Plan / step not found for the caller, or PLAN_ENGINE_DISABLED"),
            409: OpenApiResponse(description="PLAN_STEP_NOT_EXECUTABLE, details.reason"),
        },
    )
    @gated
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        try:
            plan_id = _uuid_field(data, "plan_id")
            safety = parse_step_safety_input(data)
        except SafetyInputError as exc:  # раньше ValueError: SafetyInputError — его род
            return error_response("PLAN_CONTRACT_VIOLATION", "Запрос не конформен", details={"reason": exc.reason})
        except ValueError as exc:
            return error_response(
                "PLAN_CONTRACT_VIOLATION", "Запрос не конформен", details={"reason": f"{exc}_malformed"},
            )
        step_id = data.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            return error_response(
                "PLAN_CONTRACT_VIOLATION", "Запрос не конформен", details={"reason": "step_id_missing"},
            )
        try:
            found = candidates_for_step(request.user, plan_id, step_id.strip(), safety)
        except (PlanEngineDisabled, PlanNotFound, StepNotFound, StepNotExecutable) as exc:
            return _step_refusal(exc)
        return success_response(found)


class PlanStepBookingView(APIView):
    """POST /api/v1/internal/me/plan/steps/booking/

    ``{plan_id, step_id, appointment_id, safety_state, safety_policy_version,
    evaluated_at_revision}`` — запись, сделанная от шага, как
    факт на шаге (контракт §8.3). Ни план, ни цель не меняются. 201 — связано;
    200 — повтор; 404 — план, шаг или запись не у этого человека; 409
    ``PLAN_STEP_NOT_EXECUTABLE`` (шаг не допущен, ``details.reason``) или
    ``PLAN_STEP_BOOKING_CONFLICT`` (запись уже у другого шага).
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(tags=["internal"], responses={201: OpenApiResponse(description="{plan, created}")})
    @gated
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        try:
            plan_id = _uuid_field(data, "plan_id")
            appointment_id = _uuid_field(data, "appointment_id")
        except ValueError as exc:
            return error_response("VALIDATION_ERROR", f"{exc} must be a UUID")
        step_id = data.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            return error_response("VALIDATION_ERROR", "step_id is required")
        try:
            safety = parse_step_safety_input(data)
        except SafetyInputError as exc:
            return error_response(
                "PLAN_CONTRACT_VIOLATION",
                "Действие с шагом плана требует состояния безопасности хода",
                details={"reason": exc.reason, "detail": exc.detail},
            )
        try:
            link, created = link_booking(request.user, plan_id, step_id, appointment_id, safety=safety)
        except AppointmentNotFound:
            return error_response(
                "NOT_FOUND", "Запись не найдена",
                details={"reason": "appointment_not_found"}, status_code=status.HTTP_404_NOT_FOUND,
            )
        except BookingLinkConflict:
            return error_response(
                "PLAN_STEP_BOOKING_CONFLICT",
                "Эта запись уже связана с другим шагом",
                status_code=status.HTTP_409_CONFLICT,
            )
        except (PlanEngineDisabled, PlanNotFound, StepNotFound, StepNotExecutable) as exc:
            return _step_refusal(exc)
        return success_response(
            {"plan": plan_document(link.plan), "created": created},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PlanRestrictionView(APIView):
    """POST /api/v1/internal/me/plan/restrictions/ — открыть ограничение на
    сохранённом плане (DRF-2877).

    Тело: ``{plan_id, scope: PLAN|STEP, step_id?, cause, question_id,
    safety_state, safety_policy_version, evaluated_at_revision}``. Текста
    вопроса и слов человека в теле нет.
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            201: OpenApiResponse(description="Restriction opened; body = {restriction_id, created: true, plan}"),
            200: OpenApiResponse(description="The same open restriction; created: false"),
            400: OpenApiResponse(description="PLAN_CONTRACT_VIOLATION, details.reason"),
            404: OpenApiResponse(description="Plan not found for the caller, or PLAN_ENGINE_DISABLED"),
        },
    )
    @gated
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        try:
            plan_id = _uuid_field(data, "plan_id")
            spec = parse_restriction(data)
            safety = parse_safety_input(data)
        except (RestrictionMalformed, SafetyInputError) as exc:  # раньше ValueError: SafetyInputError — его род
            return _restriction_malformed(exc.reason)
        except ValueError as exc:
            return _restriction_malformed(f"{exc}_malformed")
        try:
            row, created = open_restriction(request.user, plan_id, spec, safety)
        except RestrictionMalformed as exc:
            return _restriction_malformed(exc.reason)
        except PlanEngineDisabled:
            return _disabled()
        except PlanNotFound:
            return error_response(
                "NOT_FOUND", "План не найден",
                details={"reason": "plan_not_found"}, status_code=status.HTTP_404_NOT_FOUND,
            )
        return success_response(
            {"restriction_id": str(row.id), "created": created, "plan": plan_document(row.plan)},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


class PlanRestrictionLiftView(APIView):
    """POST /api/v1/internal/me/plan/restrictions/lift/ — снять ограничение
    (DRF-2877).

    Тело: ``{plan_id, restriction_id, lift_kind, answer_option_id?,
    safety_state, safety_policy_version, evaluated_at_revision}``.
    ``answer_option_id`` — идентификатор варианта ответа, не текст.
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            200: OpenApiResponse(description="Lifted (or already lifted); body = {lifted: true, created, plan}"),
            400: OpenApiResponse(description="PLAN_CONTRACT_VIOLATION, details.reason"),
            404: OpenApiResponse(description="Plan / restriction not found for the caller, or PLAN_ENGINE_DISABLED"),
            409: OpenApiResponse(description="PLAN_RESTRICTION_NOT_LIFTABLE, details.reason"),
        },
    )
    @gated
    def post(self, request: Request) -> Response:
        data = request.data if isinstance(request.data, dict) else {}
        try:
            plan_id = _uuid_field(data, "plan_id")
            restriction_id = _uuid_field(data, "restriction_id")
            safety = parse_safety_input(data)
        except SafetyInputError as exc:  # раньше ValueError: SafetyInputError — его род
            return _restriction_malformed(exc.reason)
        except ValueError as exc:
            return _restriction_malformed(f"{exc}_malformed")
        lift_kind = data.get("lift_kind")
        if not isinstance(lift_kind, str) or not lift_kind.strip():
            return _restriction_malformed("lift_kind_missing")
        answer = data.get("answer_option_id", "")
        if not isinstance(answer, str) or len(answer) > 128:
            return _restriction_malformed("answer_option_id_malformed")
        try:
            lift, created = lift_restriction(
                request.user, plan_id, restriction_id,
                lift_kind=lift_kind.strip(), answer_option_id=answer.strip(), safety=safety,
            )
        except PlanEngineDisabled:
            return _disabled()
        except PlanNotFound:
            return error_response(
                "NOT_FOUND", "План не найден",
                details={"reason": "plan_not_found"}, status_code=status.HTTP_404_NOT_FOUND,
            )
        except RestrictionNotFound:
            return error_response(
                "NOT_FOUND", "Ограничение не найдено",
                details={"reason": "restriction_not_found"}, status_code=status.HTTP_404_NOT_FOUND,
            )
        except RestrictionNotLiftable as exc:
            return error_response(
                "PLAN_RESTRICTION_NOT_LIFTABLE",
                "Ограничение сейчас не снимается",
                details={"reason": exc.reason},
                status_code=status.HTTP_409_CONFLICT,
            )
        return success_response(
            {"lifted": True, "created": created, "plan": plan_document(lift.restriction.plan)},
        )


def _restriction_malformed(reason: str) -> Response:
    return error_response(
        "PLAN_CONTRACT_VIOLATION", "Запрос не конформен", details={"reason": reason},
    )


class PlanDecisionView(APIView):
    """POST /api/v1/internal/me/plan/decision/

    Сборка эфемерного плана по действующей цели человека (контракт §4.1;
    DRF-2871). Ничего не сохраняет. Тело: ``{safety_state, safety_policy_version,
    rules_registry: {registry_version, rules: [...]}, excluded_capability_refs?}``
    — безопасность и реестр правил приносит вызывающий, здесь они не вычисляются.

    200 — ``{outcome, decision, safety_state, details}``; ``decision`` есть
    только при ``outcome = PLAN`` и принимается командой сохранения без
    переделки. Прочие исходы — штатные ответы, не ошибки: ``SAFETY_BLOCKED``,
    ``NO_GOAL``, ``NO_CURATED_DECOMPOSITION``, ``PLAN_NOT_JUSTIFIED``. 400
    ``PLAN_CONTRACT_VIOLATION`` — вход не конформен; 404 ``PLAN_ENGINE_DISABLED``.
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(tags=["internal"], responses={200: OpenApiResponse(description="{outcome, decision}")})
    @gated
    def post(self, request: Request) -> Response:
        try:
            result = compose_plan(request.user, parse_compose_request(request.data))
        except PlanEngineDisabled:
            return _disabled()
        except ContractViolation as exc:
            return error_response(
                "PLAN_CONTRACT_VIOLATION",
                "Запрос на сборку плана не соответствует контракту",
                details={"reason": exc.reason, "detail": exc.detail},
            )
        return success_response(result)


#: Сколько ключей за один запрос: у плана единицы шагов, сотня — уже перебор словаря.
MAX_LABEL_KEYS = 50


class PlanCapabilityLabelsView(APIView):
    """POST /api/v1/internal/me/plan/capability-labels/

    ``{keys: [...]}`` → ``{labels: {key: {state, label, expected_effect}}}``. У шага плана
    текста нет (контракт PE-2) — подпись способности берётся здесь, из
    подтверждённого знания каталога. ``state``: ``labelled`` (подпись есть) |
    ``unknown`` (подтверждённой способности с таким ключом нет) | ``no_text``
    | ``ambiguous`` (у ключа несколько разных формулировок — подписи нет).
    ``expected_effect`` — курируемый ожидаемый эффект той же записи, ответ
    на «зачем этот шаг»; ``null`` — не заполнен, и подставлять вместо него
    нечего.
    Ничего о человеке не читает и не пишет.
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(tags=["internal"], responses={200: OpenApiResponse(description="{labels}")})
    # Без гейта (wellness.plan_gate): подписи — чтение знания каталога, нужное
    # и для ПОКАЗА сохранённого плана; о человеке ручка ничего не читает и не
    # пишет. Под гейтом человек с заявкой на удаление видел бы свой план
    # списком ключей.
    def post(self, request: Request) -> Response:
        if not plan_engine_enabled():
            return _disabled()
        keys = request.data.get("keys") if isinstance(request.data, dict) else None
        if (
            not isinstance(keys, list)
            or not keys
            or len(keys) > MAX_LABEL_KEYS
            or not all(isinstance(k, str) and k.strip() for k in keys)
        ):
            return error_response(
                "VALIDATION_ERROR", f"keys must be a list of 1..{MAX_LABEL_KEYS} non-empty strings",
            )
        # DRF-2871 — подпись синтетической способности читается по тому же
        # серверному разрешению субъекта, что и сама способность при сборке;
        # иначе у шага синтетического плана не было бы текста. ``synthetic``
        # рядом с подписью — чтобы экран пометил такой шаг.
        labels = capability_labels(keys, include_synthetic=grant_for(request.user))
        return success_response(
            {
                "labels": {
                    key: {
                        "state": item.state.value, "label": item.label,
                        "expected_effect": item.expected_effect, "synthetic": item.synthetic,
                    }
                    for key, item in labels.items()
                }
            }
        )
