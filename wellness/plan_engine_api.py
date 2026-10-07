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

from users.permissions import IsBotServiceWithVerifiedClient
from users.response import error_response, success_response

from .plan_engine import (
    REQUESTABLE_STATUSES,
    ActivePlanExists,
    ContractViolation,
    GoalNotFound,
    GoalRequired,
    IdempotencyConflict,
    PlanEngineDisabled,
    PlanNotFound,
    TransitionRefused,
    create_plan_from_command,
    parse_command,
    plan_document,
    plan_payload,
    set_plan_status,
)
from .plan_engine_steps import (
    AppointmentNotFound,
    BookingLinkConflict,
    ResolutionRefused,
    StepNotExecutable,
    StepNotFound,
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
        return success_response({"plan": plan_payload(request.user)})

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
        except IdempotencyConflict:
            return error_response(
                "PLAN_IDEMPOTENCY_CONFLICT",
                "Это подтверждение уже использовано для другого плана",
                status_code=status.HTTP_409_CONFLICT,
            )
        plan.refresh_from_db()
        return success_response(
            {"plan": plan_document(plan), "created": created},
            status_code=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )


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
            details={"reason": exc.reason},
            status_code=status.HTTP_409_CONFLICT,
        )
    raise exc


class PlanStepResolutionView(APIView):
    """POST /api/v1/internal/me/plan/steps/resolution/

    ``{plan_id, step_id, level: SERVICE | OFFER, canonical_service_ref,
    tenant_offer_ref?, resolver_decision_id}`` — чем резолвер разрешил шаг
    (контракт §4.3, §8.2). 201 — записано; 200 — повтор того же перехода; 409
    ``PLAN_STEP_RESOLUTION_REFUSED`` / ``PLAN_STEP_NOT_EXECUTABLE`` с
    ``details.reason``. ``recommendation_id`` не принимается (§8.3).
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(tags=["internal"], responses={201: OpenApiResponse(description="{plan, created}")})
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
            resolution, created = resolve_step(
                request.user,
                plan_id,
                step_id,
                level=data.get("level"),
                canonical_service_ref=canonical,
                tenant_offer_ref=offer,
                resolver_decision_id=data.get("resolver_decision_id"),
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


class PlanStepBookingView(APIView):
    """POST /api/v1/internal/me/plan/steps/booking/

    ``{plan_id, step_id, appointment_id}`` — запись, сделанная от шага, как
    факт на шаге (контракт §8.3). Ни план, ни цель не меняются. 201 — связано;
    200 — повтор; 404 — план, шаг или запись не у этого человека; 409
    ``PLAN_STEP_NOT_EXECUTABLE`` (шаг не допущен, ``details.reason``) или
    ``PLAN_STEP_BOOKING_CONFLICT`` (запись уже у другого шага).
    """

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(tags=["internal"], responses={201: OpenApiResponse(description="{plan, created}")})
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
            link, created = link_booking(request.user, plan_id, step_id, appointment_id)
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
