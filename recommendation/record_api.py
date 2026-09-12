"""Чтение записи Recommendation и события взаимодействия — для бота (C04.1).

``GET  /api/v1/internal/users/{user_id}/recommendations/{set_id}/``
``POST /api/v1/internal/users/{user_id}/recommendations/{recommendation_id}/events/``

Оба — под ``IsInternalBearerForSubject`` (DRF-1617 / B-2.1): субъект в URL =
человек, чей набор, и он же обязан стоять в ``X-External-User-ID``. Набор
чужого субъекта — 404, не 403: «нет такого» для того, кто не вправе знать.
``RecommendationSet.subject_ref`` — UUID пользователя Ayla строкой (уже
псевдоним; ни телефона, ни имени).

Что отдаётся (просьба e8 к #426):

* ``actionable`` — ``now < actionable_until`` **на момент ответа**: канал
  срок не считает; после 2 ч — ``false``, запись при этом на месте (B13);
* ``why`` — **только** ``explanation.user_visible_reasons`` и только при
  ``explanation.displayable = True``; ``internal_only`` не отдаётся ни в
  каком поле (контракт §12–§13; owner ruling 2026-07-29). Сторож в тестах
  ищет internal-only текст во всём теле ответа;
* primary + alternatives с ``parent`` и ``rerank_reason`` (код словаря
  канона v1.1 §10.3 — не фраза; фраза альтернативы — её собственный ``why``);
* ``execution_mode`` набора (C1): бот не показывает ``SHADOW``.

Услуги, мастера, цены, слота в ответе нет — их в записи нет по построению
(B2/B3); это C05 из ExecutionOption.

События: ``presented / explanation_requested / alternative_requested /
engaged`` — пишет канал; ``booking_intent.created`` — Booking / Handoff, не
эта ручка (отказ по имени). ``accepted`` / ``declined`` → 400 с кодом
``EVENT_NOT_IN_TAXONOMY`` (B8).
"""
from __future__ import annotations

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone

from recommendation.models import Recommendation, RecommendationEvent, RecommendationSet
from recommendation.records import RecordInvalid, record_event
from users.permissions import IsInternalBearerForSubject
from users.response import error_response, success_response

#: Что бот вправе записать этой ручкой. ``created`` пишет persist,
#: ``booking_intent.created`` — Booking / Handoff (контракт §15).
CHANNEL_EVENT_KINDS = (
    RecommendationEvent.Kind.PRESENTED,
    RecommendationEvent.Kind.EXPLANATION_REQUESTED,
    RecommendationEvent.Kind.ALTERNATIVE_REQUESTED,
    RecommendationEvent.Kind.ENGAGED,
)
_B8_REFUSED = ("recommendation.accepted", "recommendation.declined", "accepted", "declined")


def _why(rec: Recommendation) -> list[str]:
    """Только displayable и только user_visible_reasons. internal_only не покидает модель."""
    exp = rec.explanation or {}
    if not exp.get("displayable"):
        return []
    return [str(s) for s in exp.get("user_visible_reasons", []) if str(s).strip()]


def _record(rec: Recommendation, now) -> dict:
    return {
        "recommendation_id": str(rec.pk),
        "role": rec.role,
        "parent_recommendation_id": str(rec.parent_id) if rec.parent_id else None,
        "rerank_reason": rec.rerank_reason or None,
        "supersedes_recommendation_id": str(rec.supersedes_id) if rec.supersedes_id else None,
        "decision_subject": {
            "direction_code": rec.direction_code,
            "family": rec.family,
            "target_outcomes": list(rec.target_outcomes or []),
        },
        "result_status": rec.result_status,
        "readiness_state": rec.readiness_state,
        "reason_codes": list(rec.reason_codes or []),
        "displayable": bool((rec.explanation or {}).get("displayable")),
        "why": _why(rec),
        "safety_state": (rec.safety_evaluation_ref or {}).get("state"),
        "created_at": rec.created_at.isoformat(),
        "actionable_until": rec.actionable_until.isoformat(),
        "actionable": rec.is_actionable(now),
        "presentation_version": rec.presentation_version,
        "record_schema_version": rec.record_schema_version,
    }


class RecommendationSetReadSerializer(serializers.Serializer):
    """Форма ответа — для схемы. Тело собирается в `_record`, здесь — описание."""

    recommendation_set_id = serializers.UUIDField()
    subject_id = serializers.UUIDField()
    intent_id = serializers.CharField()
    execution_mode = serializers.CharField()
    created_at = serializers.DateTimeField()
    primary = serializers.DictField()
    alternatives = serializers.ListField(child=serializers.DictField())


class RecommendationEventInSerializer(serializers.Serializer):
    kind = serializers.CharField()
    channel = serializers.CharField(required=False, allow_blank=True)
    channel_message_id = serializers.CharField(required=False, allow_blank=True)
    occurred_at = serializers.DateTimeField(required=False)


class InternalRecommendationSetView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSubject]
    subject_url_kwarg = "user_id"

    @extend_schema(
        tags=["internal-recommendation"],
        responses={
            200: RecommendationSetReadSerializer,
            403: OpenApiResponse(description="Missing / invalid bearer, unnamed or foreign subject (DRF-1617)"),
            404: OpenApiResponse(description="No such recommendation set for this subject"),
        },
    )
    def get(self, request: Request, user_id, set_id) -> Response:
        rset = (
            RecommendationSet.objects.filter(pk=set_id, subject_ref=str(user_id))
            .prefetch_related("recommendations")
            .first()
        )
        if rset is None:
            return error_response("NOT_FOUND", "Recommendation set not found.", status_code=404)
        now = timezone.now()
        recs = list(rset.recommendations.order_by("created_at", "pk"))
        primary = next((r for r in recs if r.role == Recommendation.Role.PRIMARY), None)
        alternatives = [r for r in recs if r.role == Recommendation.Role.ALTERNATIVE]
        return success_response({
            "recommendation_set_id": str(rset.pk),
            "subject_id": rset.subject_ref,
            "intent_id": rset.intent_id,
            "semantic_resolution_ref": rset.semantic_resolution_ref or None,
            "execution_mode": rset.execution_mode,
            "conversation_ref": dict(rset.conversation_ref or {}),
            "created_at": rset.created_at.isoformat(),
            "primary": _record(primary, now) if primary else None,
            "alternatives": [_record(r, now) for r in alternatives],
        })


class InternalRecommendationEventView(APIView):
    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSubject]
    subject_url_kwarg = "user_id"

    @extend_schema(
        tags=["internal-recommendation"],
        request=RecommendationEventInSerializer,
        responses={
            201: OpenApiResponse(description="Event appended"),
            400: OpenApiResponse(description="EVENT_NOT_IN_TAXONOMY (accepted/declined — B8) | VALIDATION_ERROR"),
            404: OpenApiResponse(description="No such recommendation for this subject"),
        },
    )
    def post(self, request: Request, user_id, recommendation_id) -> Response:
        rec = (
            Recommendation.objects.select_related("recommendation_set")
            .filter(pk=recommendation_id, recommendation_set__subject_ref=str(user_id))
            .first()
        )
        if rec is None:
            return error_response("NOT_FOUND", "Recommendation not found.", status_code=404)
        ser = RecommendationEventInSerializer(data=request.data)
        if not ser.is_valid():
            return error_response("VALIDATION_ERROR", "Invalid event.", details=ser.errors, status_code=400)
        kind = ser.validated_data["kind"]
        if kind in _B8_REFUSED:
            return error_response(
                "EVENT_NOT_IN_TAXONOMY",
                "recommendation.accepted / declined do not exist (B8): use recommendation.engaged for "
                "interaction, booking_intent.created for execution, reaction REJECTED for refusal.",
                details={"kind": kind}, status_code=400,
            )
        if kind not in CHANNEL_EVENT_KINDS:
            return error_response(
                "VALIDATION_ERROR",
                f"kind must be one of {[k.value for k in CHANNEL_EVENT_KINDS]}",
                details={"kind": kind}, status_code=400,
            )
        payload = {k: v for k, v in ser.validated_data.items() if k in ("channel", "channel_message_id") and v}
        try:
            ev = record_event(rec, kind, payload=payload, at=ser.validated_data.get("occurred_at"))
        except RecordInvalid as exc:
            return error_response("VALIDATION_ERROR", str(exc), status_code=400)
        return success_response({
            "event_id": str(ev.pk), "kind": ev.kind, "recommendation_id": str(rec.pk),
            "occurred_at": ev.occurred_at.isoformat(),
        }, status_code=201)
