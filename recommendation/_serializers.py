"""Схема границы — контракт §9.4. Обязательна на ОБОИХ концах.

Здесь стоит серверная половина. Вход разбирается схемой, и выход **тоже
проверяется схемой перед отправкой**: источник, отдавший форму, которую сам
же не объявлял, — это не «странный ответ», это нарушение договора, и узнать
о нём должен тот, кто его нарушил, а не тот, кто его получил.

Три запрета выражены формой, а не дисциплиной:

* **строки для показа нет.** В ответе нет полей `reasoning_text` /
  `reason_text` / `why_text`: потребитель получает коды и собирает фразу сам
  (§7). Раньше строку собирал источник, и она врала — «Рейтинг 4.9» при нуле
  отзывов;
* **сырого балла нет** (канон §8). Наружу идут `tier` и `rank`;
* **рейтинг не сериализуется без числа отзывов** (§8.4 E2) — значение
  свидетельства о рейтинге это пара, и другой формы у него нет.
"""
from __future__ import annotations

from rest_framework import serializers

from ._evidence import EvidenceItem, RatingValue
from ._reason_codes import ReasonCode
from ._types import (
    CandidateKind,
    Constraint,
    ConstraintKind,
    NeedOrigin,
    NeedSpec,
    RecommendationDecision,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
    UserConstraints,
)

#: Поля, которых в ответе границы быть не может. Проверяется тестом W1:
#: имя, добавленное завтра, сломает сегодняшний прогон.
FORBIDDEN_RESPONSE_FIELDS = frozenset({"reasoning_text", "reason_text", "why_text", "score", "match_score"})


# ---------------------------------------------------------------------------
# Вход
# ---------------------------------------------------------------------------

class _ConstraintSerializer(serializers.Serializer):
    """Трёхзначность на проводе: `null` не схлопывает UNKNOWN и FLEXIBLE.

    Поэтому ограничение едет объектом `{"kind": ..., "value": ...}`, а не
    голым значением: голое `null` означало бы сразу и «не спросили», и
    «человеку всё равно», а это разные ответы (канон §16.2).
    """

    kind = serializers.ChoiceField(choices=[c.value for c in ConstraintKind])
    value = serializers.JSONField(required=False, allow_null=True, default=None)

    def to_constraint(self, data: dict) -> Constraint:
        kind = ConstraintKind(data["kind"])
        return Constraint(kind, data.get("value") if kind is ConstraintKind.KNOWN else None)


class _ScopeSerializer(serializers.Serializer):
    mode = serializers.ChoiceField(choices=[m.value for m in ScopeMode])
    tenant_refs = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    exclude_tenant_refs = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    city = serializers.CharField(required=False, allow_null=True, default=None)
    radius_km = serializers.FloatField(required=False, allow_null=True, default=None)


class _NeedSerializer(serializers.Serializer):
    origin = serializers.ChoiceField(choices=[o.value for o in NeedOrigin])
    capability_refs = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    canonical_service_refs = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    goal_key = serializers.CharField(required=False, allow_null=True, default=None)
    raw_text = serializers.CharField(required=False, allow_null=True, default=None)


class ResolveRequestSerializer(serializers.Serializer):
    """Вход `POST /api/v1/internal/recommendation/resolve/`.

    `subject_ref` тут **нет намеренно**: кого спрашивают, определяет
    аутентификация, а не тело запроса. Иначе вызывающий мог бы получить
    решение «за другого человека», и граница, которая должна была отнять
    политику, раздала бы личный контекст.
    """

    request_id = serializers.CharField(max_length=128)
    surface = serializers.ChoiceField(choices=[s.value for s in Surface])
    scope = _ScopeSerializer()
    need = _NeedSerializer()
    price_max = _ConstraintSerializer(required=False)
    time_window = _ConstraintSerializer(required=False)
    provider_ref = _ConstraintSerializer(required=False)
    safety_state = serializers.ChoiceField(
        choices=[s.value for s in SafetyState], required=False, default=SafetyState.UNKNOWN.value,
    )
    tie_break_seed = serializers.CharField(required=False, allow_null=True, default=None)
    k = serializers.IntegerField(required=False, min_value=1, max_value=50, default=3)

    def build_scope(self) -> Scope:
        raw = self.validated_data["scope"]
        return Scope(
            mode=ScopeMode(raw["mode"]),
            tenant_refs=tuple(raw.get("tenant_refs") or ()),
            exclude_tenant_refs=tuple(raw.get("exclude_tenant_refs") or ()),
            city=raw.get("city"),
            radius_km=raw.get("radius_km"),
        )

    def build_need(self) -> NeedSpec:
        raw = self.validated_data["need"]
        return NeedSpec(
            origin=NeedOrigin(raw["origin"]),
            capability_refs=tuple(raw.get("capability_refs") or ()),
            canonical_service_refs=tuple(raw.get("canonical_service_refs") or ()),
            goal_key=raw.get("goal_key"),
            raw_text=raw.get("raw_text"),
        )

    def build_constraints(self) -> UserConstraints:
        helper = _ConstraintSerializer()
        fields = {}
        for name in ("price_max", "time_window", "provider_ref"):
            raw = self.validated_data.get(name)
            fields[name] = helper.to_constraint(raw) if raw else Constraint.unknown()
        return UserConstraints(**fields)


# ---------------------------------------------------------------------------
# Выход
# ---------------------------------------------------------------------------

class _CandidateRefSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=[k.value for k in CandidateKind])
    id = serializers.UUIDField()


class _EvidenceSerializer(serializers.Serializer):
    kind = serializers.CharField()
    strength = serializers.CharField()
    origin = serializers.CharField()
    value = serializers.JSONField(allow_null=True)
    observed_at = serializers.DateTimeField(allow_null=True)
    source_ref = serializers.CharField(allow_null=True)


class _RankedCandidateSerializer(serializers.Serializer):
    candidate = _CandidateRefSerializer()
    rank = serializers.IntegerField()
    tier = serializers.IntegerField()
    reason_codes = serializers.ListField(child=serializers.CharField())
    evidence = _EvidenceSerializer(many=True)
    stage_verdicts = serializers.DictField(child=serializers.CharField())


class _ExcludedCandidateSerializer(serializers.Serializer):
    candidate = _CandidateRefSerializer()
    stage = serializers.CharField()
    reason_code = serializers.CharField()


class _StageActivitySerializer(serializers.Serializer):
    stage = serializers.CharField()
    active = serializers.BooleanField()
    reason = serializers.CharField(allow_null=True)


class _PolicyVersionsSerializer(serializers.Serializer):
    resolver_spec_version = serializers.CharField()
    stage_policy_version = serializers.CharField()
    reason_code_registry_version = serializers.CharField()
    catalog_mapping_version = serializers.CharField(allow_null=True)
    safety_policy_version = serializers.CharField(allow_null=True)
    tie_break_policy_version = serializers.CharField(allow_null=True)


class ResolveResponseSerializer(serializers.Serializer):
    """Форма ответа. У неё есть владелец, и владелец — этот файл (§2.1 C3)."""

    decision_id = serializers.CharField()
    request_id = serializers.CharField()
    resolver_spec_version = serializers.CharField()
    ordered = _RankedCandidateSerializer(many=True)
    excluded = _ExcludedCandidateSerializer(many=True)
    stage_activity = _StageActivitySerializer(many=True)
    reason_codes = serializers.ListField(child=serializers.CharField())
    policy_versions = _PolicyVersionsSerializer()
    computed_at = serializers.DateTimeField()


def decision_to_payload(decision: RecommendationDecision) -> dict:
    """Разложить решение в тело ответа. Ни строки для показа, ни балла."""
    return {
        "decision_id": decision.decision_id,
        "request_id": decision.request_id,
        "resolver_spec_version": decision.policy_versions.resolver_spec_version,
        "ordered": [
            {
                "candidate": {"kind": c.candidate_ref.kind.value, "id": str(c.candidate_ref.id)},
                "rank": c.rank,
                "tier": c.tier,
                "reason_codes": [code.value for code in c.reason_codes],
                "evidence": [_evidence_to_payload(item) for item in c.evidence],
                "stage_verdicts": {stage.value: verdict.value for stage, verdict in c.stage_verdicts.items()},
            }
            for c in decision.ordered
        ],
        "excluded": [
            {
                "candidate": {"kind": e.candidate_ref.kind.value, "id": str(e.candidate_ref.id)},
                "stage": e.excluded_at_stage.value,
                "reason_code": e.reason_code.value,
            }
            for e in decision.excluded
        ],
        "stage_activity": [
            {"stage": row.stage.value, "active": row.active, "reason": row.reason}
            for row in decision.stage_activity
        ],
        "reason_codes": [code.value for code in decision.reason_codes],
        "policy_versions": {
            "resolver_spec_version": decision.policy_versions.resolver_spec_version,
            "stage_policy_version": decision.policy_versions.stage_policy_version,
            "reason_code_registry_version": decision.policy_versions.reason_code_registry_version,
            "catalog_mapping_version": decision.policy_versions.catalog_mapping_version,
            "safety_policy_version": decision.policy_versions.safety_policy_version,
            "tie_break_policy_version": decision.policy_versions.tie_break_policy_version,
        },
        "computed_at": decision.computed_at.isoformat(),
    }


def _evidence_to_payload(item: EvidenceItem) -> dict:
    """Свидетельство на проводе. Рейтинг — всегда пара, иначе никак (E2)."""
    if isinstance(item.value, RatingValue):
        value = {"rating": str(item.value.rating), "review_count": item.value.review_count}
    elif isinstance(item.value, ReasonCode):
        value = item.value.value
    elif hasattr(item.value, "value"):
        value = item.value.value
    else:
        value = item.value
    return {
        "kind": item.kind.value,
        "strength": item.strength.value,
        "origin": item.origin.value,
        "value": value,
        "observed_at": item.observed_at.isoformat() if item.observed_at else None,
        "source_ref": item.source_ref,
    }
