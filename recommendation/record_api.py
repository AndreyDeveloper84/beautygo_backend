"""Запись Recommendation для бота (C04.1): вход производителя NBA, чтение набора, события.

``POST /api/v1/internal/users/{user_id}/recommendation-sets/``
``GET  /api/v1/internal/users/{user_id}/recommendations/{set_id}/``
``POST /api/v1/internal/users/{user_id}/recommendations/{recommendation_id}/events/``

Все три — под ``IsInternalBearerForSubject`` (DRF-1617 / B-2.1): субъект в URL =
человек, чей набор, и он же обязан стоять в ``X-External-User-ID``. Набор
чужого субъекта — 404, не 403: «нет такого» для того, кто не вправе знать.
``RecommendationSet.subject_ref`` — UUID пользователя Ayla строкой (уже
псевдоним; ни телефона, ни имени).

Вход записи (DRF-1888)
----------------------

**Рабочий, но пустой вход до производителя NBA (мозг, срез 6).** Резолвер
каталога запись не пишет и писать не будет: он ранжирует исполнителей (сегмент
исполнения, контракт §5 этап 17), а запись — это решение WHAT (B2/B10;
замер ``docs/MEASURE_RECOMMENDATION_RECORD_LIVE_PATH_2026-09-15.md``). Решение
приносит тот, кто его принял; здесь — только проверка и запись:

* проверка — тот же ``records.persist`` (минимум §5, версии политик, lineage);
  лишнее поле во входе — отказ по имени, а не молчаливый пропуск
  (``service_id`` в записи NBA не место, B2);
* ``execution_mode`` ставит **сервер**: ``SHADOW`` (C1). ``LIVE`` — 400
  ``LIVE_NOT_ALLOWED``, пока пороги DecisionReadiness не доказаны (C1, O3);
* живая заявка на удаление — 423 до любой записи (§7 D2);
* повтор — ``X-Idempotency-Key`` обязателен (тот же механизм, что у записи и
  отмены визита бота, ``appointments.infrastructure.idempotency``): тот же ключ
  и тело — тот же ответ без второй записи; тот же ключ с другим телом — 422
  ``IDEMPOTENCY_CONFLICT``. Запись immutable, поэтому дубль не исправить задним
  числом — его можно только не создать. Бот ставит ключ ``intent_id:trace_id``;
* ссылка на решение резолвера — только ``execution_mapping_snapshot_ref``
  (ссылка, не копия, B10).

Что отдаётся при чтении (просьба e8 к #426):

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

from dataclasses import fields as dataclass_fields
from uuid import UUID

from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from django.utils import timezone

from appointments.infrastructure.idempotency import (
    IdempotencyConflict,
    IdempotencyInFlight,
    lookup_or_open_idempotency,
    record_response,
)
from recommendation.models import ExecutionMode, Recommendation, RecommendationEvent, RecommendationSet
from recommendation.records import (
    PolicyVersions,
    RecommendationInput,
    RecommendationSetInput,
    RecordInvalid,
    persist,
    record_event,
)
from users.deletion_requests import deletion_block_for, deletion_refusal
from users.models import User
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

#: Операция в таблице ключей идемпотентности — своя, чтобы ключ бота для
#: записи визита не совпал с ключом записи рекомендации.
IDEMPOTENCY_OPERATION = "recommendation_set.create"

_RECORD_FIELDS = frozenset(f.name for f in dataclass_fields(RecommendationInput))
_VERSION_FIELDS = tuple(f.name for f in dataclass_fields(PolicyVersions))


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


class RecommendationSetCreateSerializer(serializers.Serializer):
    """Вход записи: форма верхнего уровня. Минимум §5 проверяет ``persist``."""

    intent_id = serializers.CharField(max_length=64)
    semantic_resolution_ref = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    #: Только чтобы назвать отказ: значение ставит сервер (C1).
    execution_mode = serializers.CharField(required=False, allow_blank=True, default="")
    conversation_ref = serializers.DictField(required=False, default=dict)
    versions = serializers.DictField()
    primary = serializers.DictField()
    alternatives = serializers.ListField(child=serializers.DictField(), required=False, default=list)


class RecommendationSetCreatedSerializer(serializers.Serializer):
    recommendation_set_id = serializers.UUIDField()
    primary_recommendation_id = serializers.UUIDField()
    alternative_recommendation_ids = serializers.ListField(child=serializers.UUIDField())
    execution_mode = serializers.CharField()


class RecommendationEventInSerializer(serializers.Serializer):
    kind = serializers.CharField()
    channel = serializers.CharField(required=False, allow_blank=True)
    channel_message_id = serializers.CharField(required=False, allow_blank=True)
    occurred_at = serializers.DateTimeField(required=False)


def _record_input(raw: dict, label: str) -> RecommendationInput:
    """Словарь входа → ``RecommendationInput``. Лишний ключ — отказ по имени."""
    extra = sorted(set(raw) - _RECORD_FIELDS)
    if extra:
        raise RecordInvalid(f"{label}: поля {extra} не входят в запись Recommendation (B2: WHAT, не HOW/WHO)")
    data = dict(raw)
    for name in ("parent_id", "supersedes_id"):
        if data.get(name) not in (None, ""):
            try:
                data[name] = UUID(str(data[name]))
            except ValueError as exc:
                raise RecordInvalid(f"{label}: {name} — не UUID") from exc
        else:
            data.pop(name, None)
    try:
        return RecommendationInput(**data)
    except TypeError as exc:   # нет обязательного поля
        raise RecordInvalid(f"{label}: {exc}") from exc


def _set_input(subject: User, data: dict) -> RecommendationSetInput:
    versions = data["versions"]
    extra = sorted(set(versions) - set(_VERSION_FIELDS))
    if extra:
        raise RecordInvalid(f"versions: поля {extra} не входят в провенанс")
    return RecommendationSetInput(
        subject_ref=str(subject.pk),
        intent_id=data["intent_id"],
        semantic_resolution_ref=data["semantic_resolution_ref"],
        execution_mode=ExecutionMode.SHADOW,
        conversation_ref=dict(data["conversation_ref"]),
        versions=PolicyVersions(**{name: str(versions.get(name) or "") for name in _VERSION_FIELDS}),
        primary=_record_input(data["primary"], "primary"),
        alternatives=tuple(
            _record_input(raw, f"alternative[{i}]") for i, raw in enumerate(data["alternatives"], start=1)
        ),
    )


class InternalRecommendationSetCreateView(APIView):
    """Вход производителя NBA — см. докстринг модуля, раздел «Вход записи»."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearerForSubject]
    subject_url_kwarg = "user_id"

    @extend_schema(
        tags=["internal-recommendation"],
        request=RecommendationSetCreateSerializer,
        responses={
            201: RecommendationSetCreatedSerializer,
            400: OpenApiResponse(
                description=(
                    "IDEMPOTENCY_KEY_REQUIRED | LIVE_NOT_ALLOWED (C1) | "
                    "VALIDATION_ERROR (минимум §5, по имени поля)"
                ),
            ),
            403: OpenApiResponse(description="Missing / invalid bearer, unnamed or foreign subject (DRF-1617)"),
            404: OpenApiResponse(description="No such user"),
            409: OpenApiResponse(description="IDEMPOTENCY_IN_FLIGHT"),
            422: OpenApiResponse(description="IDEMPOTENCY_CONFLICT — тот же ключ, другое тело"),
            423: OpenApiResponse(description="DELETION_IN_PROGRESS (§7 D2)"),
        },
    )
    def post(self, request: Request, user_id) -> Response:
        subject = User.objects.filter(pk=user_id).first()
        if subject is None:
            return error_response("NOT_FOUND", "User not found.", status_code=404)
        # D2: отказ по воле человека раньше ключа и формы — и в кэш ответов
        # не попадает: повтор после отзыва заявки обязан записать.
        blocked = deletion_block_for(subject)
        if blocked is not None:
            return deletion_refusal(blocked)
        if not (request.META.get("HTTP_X_IDEMPOTENCY_KEY") or "").strip():
            return error_response(
                "IDEMPOTENCY_KEY_REQUIRED",
                "X-Idempotency-Key is required: the record is immutable, a duplicate cannot be undone.",
                status_code=400,
            )
        try:
            cached, idem = lookup_or_open_idempotency(
                request, user=subject, operation_name=IDEMPOTENCY_OPERATION,
                target_type="RecommendationSet", target_id=str(subject.pk),
            )
        except IdempotencyConflict:
            return error_response(
                "IDEMPOTENCY_CONFLICT", "X-Idempotency-Key reused with a different body.", status_code=422,
            )
        except IdempotencyInFlight:
            return error_response(
                "IDEMPOTENCY_IN_FLIGHT", "Same key already being processed; retry in a moment.", status_code=409,
            )
        if cached is not None:
            return Response(cached["payload"], status=cached["status"])

        response = self._create(request, subject)
        record_response(idem, response.status_code, response.data)
        return response

    @staticmethod
    def _create(request: Request, subject: User) -> Response:
        ser = RecommendationSetCreateSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid recommendation set.", details=ser.errors, status_code=400,
            )
        data = ser.validated_data
        mode = (data["execution_mode"] or "").strip()
        if mode and mode != ExecutionMode.SHADOW:
            return error_response(
                "LIVE_NOT_ALLOWED",
                "execution_mode is set by the server: SHADOW until DecisionReadiness thresholds are proven (C1).",
                details={"execution_mode": mode}, status_code=400,
            )
        try:
            rset = persist(_set_input(subject, data))
        except RecordInvalid as exc:
            return error_response("VALIDATION_ERROR", str(exc), status_code=400)
        recs = list(rset.recommendations.order_by("created_at", "pk"))
        return success_response({
            "recommendation_set_id": str(rset.pk),
            "primary_recommendation_id": next(str(r.pk) for r in recs if r.role == Recommendation.Role.PRIMARY),
            "alternative_recommendation_ids": [
                str(r.pk) for r in recs if r.role == Recommendation.Role.ALTERNATIVE
            ],
            "execution_mode": rset.execution_mode,
        }, status_code=201)


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
