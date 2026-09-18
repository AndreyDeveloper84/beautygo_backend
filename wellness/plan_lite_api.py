"""Internal bot-service API — Plan Lite writer (DRF-2101).

``/api/v1/internal/me/plan-lite/`` — тот же auth-паттерн, что у
wellness-context (DRF-1344): Bearer service token + X-External-User-ID,
разрешённый в ``request.user`` через ``IsBotServiceWithVerifiedClient``.

- ``POST`` — составить план: ``{goal_id?, actions:[{action_type, cadence,
  target_count}]}`` (1–3 действия; ``goal_id`` необязателен — без него
  активная цель вызывающего, PR-1b). 201 — создан; 409
  ``PLAN_LITE_ALREADY_ACTIVE`` — активный уже есть; 404 ``NOT_FOUND`` —
  цель не у этого человека / не активна / активной цели нет; 400 — форма.
- ``DELETE`` — закрыть активный план (append-only). 404 — активного нет.
- Флаг выключен → 404 ``PLAN_LITE_DISABLED`` по замыслу, не 5xx.

Чтение плана — полем ``plan_lite`` в документе wellness-context, отдельной
GET-ручки нет: у бота уже есть один читатель.
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

from .plan_lite import (
    GoalNotFound,
    InvalidActions,
    NoActivePlan,
    PlanAlreadyActive,
    PlanLiteDisabled,
    close_plan,
    create_plan,
    parse_actions,
    plan_lite_payload,
)


def _disabled() -> Response:
    return error_response(
        "PLAN_LITE_DISABLED",
        "Plan Lite выключен (PLAN_LITE_ENABLED)",
        status_code=status.HTTP_404_NOT_FOUND,
    )


class PlanLiteView(APIView):
    """POST / DELETE /api/v1/internal/me/plan-lite/"""

    authentication_classes: list = []
    permission_classes = [IsBotServiceWithVerifiedClient]

    @extend_schema(
        tags=["internal"],
        responses={
            201: OpenApiResponse(description="Plan created; body = plan_lite document"),
            400: OpenApiResponse(description="Malformed goal_id / actions"),
            404: OpenApiResponse(description="Goal not found for the caller, or PLAN_LITE_DISABLED"),
            409: OpenApiResponse(description="An active plan already exists"),
        },
    )
    def post(self, request: Request) -> Response:
        raw_goal = request.data.get("goal_id") if isinstance(request.data, dict) else None
        goal_id: UUID | None = None
        if raw_goal not in (None, ""):
            try:
                goal_id = UUID(str(raw_goal))
            except (TypeError, ValueError):
                return error_response("VALIDATION_ERROR", "goal_id must be a UUID")
        try:
            actions = parse_actions(request.data.get("actions"))
        except InvalidActions as exc:
            return error_response("VALIDATION_ERROR", str(exc))
        try:
            create_plan(request.user, goal_id=goal_id, actions=actions)
        except PlanLiteDisabled:
            return _disabled()
        except GoalNotFound:
            return error_response(
                "NOT_FOUND",
                "Активная цель не найдена",
                details={"reason": "no_active_goal" if goal_id is None else "goal_not_found"},
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except PlanAlreadyActive:
            return error_response(
                "PLAN_LITE_ALREADY_ACTIVE",
                "Активный план уже есть — закройте его, чтобы составить новый",
                status_code=status.HTTP_409_CONFLICT,
            )
        return success_response(
            plan_lite_payload(request.user), status_code=status.HTTP_201_CREATED,
        )

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: OpenApiResponse(description="Plan closed (append-only)"),
            404: OpenApiResponse(description="No active plan, or PLAN_LITE_DISABLED"),
        },
    )
    def delete(self, request: Request) -> Response:
        try:
            plan = close_plan(request.user)
        except PlanLiteDisabled:
            return _disabled()
        except NoActivePlan:
            return error_response(
                "NOT_FOUND", "Активного плана нет", status_code=status.HTTP_404_NOT_FOUND,
            )
        return success_response({
            "plan_id": str(plan.id),
            "status": plan.status,
            "closed_at": plan.closed_at.isoformat() if plan.closed_at else None,
        })
