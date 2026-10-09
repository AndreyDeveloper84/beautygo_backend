"""DRF-2857 — durable Plan routes (mounted at /api/v1/internal/me/plan/)."""
from django.urls import path

from .plan_engine_api import (
    PlanCapabilityLabelsView,
    PlanDecisionView,
    PlanEngineStateView,
    PlanEngineView,
    PlanReplaceView,
    PlanStepBookingView,
    PlanStepCandidatesView,
    PlanRestrictionLiftView,
    PlanRestrictionView,
    PlanStepResolutionView,
)

urlpatterns = [
    path("", PlanEngineView.as_view(), name="me-plan"),
    path("state/", PlanEngineStateView.as_view(), name="me-plan-state"),
    # DRF-2857 — подтверждённая замена действующего плана предложением.
    path("replace/", PlanReplaceView.as_view(), name="me-plan-replace"),
    # DRF-2871 — сборка эфемерного плана; ничего не сохраняет.
    path("decision/", PlanDecisionView.as_view(), name="me-plan-decision"),
    # DRF-2871 — подписи способностей для экрана плана (у шага текста нет).
    path("capability-labels/", PlanCapabilityLabelsView.as_view(), name="me-plan-capability-labels"),
    # DRF-2868 — шаг: чем разрешён и какая запись от него сделана.
    # DRF-2868 — кандидаты услуги для шага: подбор по способности, допуск, здоровье.
    path("steps/candidates/", PlanStepCandidatesView.as_view(), name="me-plan-step-candidates"),
    path("steps/resolution/", PlanStepResolutionView.as_view(), name="me-plan-step-resolution"),
    path("steps/booking/", PlanStepBookingView.as_view(), name="me-plan-step-booking"),
    # DRF-2877 — стойкий вопрос: открыть и снять ограничение плана.
    path("restrictions/", PlanRestrictionView.as_view(), name="me-plan-restriction"),
    path("restrictions/lift/", PlanRestrictionLiftView.as_view(), name="me-plan-restriction-lift"),
]
