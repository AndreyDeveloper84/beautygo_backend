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

import logging
from dataclasses import dataclass
from uuid import UUID

from rest_framework import serializers

from ._evidence import EvidenceItem, RatingValue
from ._reason_codes import ReasonCode
from ._types import (
    CandidateKind,
    Constraint,
    ConstraintKind,
    NeedOrigin,
    MAX_PREFERENCES,
    MEMORY_SOURCE_GLOBAL_BOT,
    NeedSpec,
    Preference,
    PreferenceKind,
    PreferenceOrigin,
    PreferenceStrength,
    RecommendationDecision,
    SafetyState,
    Scope,
    ScopeMode,
    SeparationState,
    StageId,
    Surface,
    UserConstraints,
)

#: Поля, которых в ответе границы быть не может. Проверяется тестом W1:
#: имя, добавленное завтра, сломает сегодняшний прогон.
logger = logging.getLogger(__name__)

#: Основа имени короче этого совпала бы с половиной салона; у бота тот же минимум.
NAME_STEM_MIN_LENGTH = 3
MAX_NAME_STEMS = 4

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

    def validate(self, attrs: dict) -> dict:
        # O-1b: «свои салоны» выводит сервер. Список от вызывающего здесь —
        # не подсказка, а попытка назвать чужую область своей (DRF-1626):
        # отказ, а не молчаливое игнорирование.
        if attrs["mode"] == ScopeMode.OWN_SALONS.value and (
            attrs.get("tenant_refs") or attrs.get("exclude_tenant_refs")
        ):
            raise serializers.ValidationError(
                "OWN_SALONS: tenant_refs и exclude_tenant_refs не принимаются — салоны клиента определяет сервер"
            )
        return attrs


class _NeedSerializer(serializers.Serializer):
    origin = serializers.ChoiceField(choices=[o.value for o in NeedOrigin])
    capability_refs = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    canonical_service_refs = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    goal_key = serializers.CharField(required=False, allow_null=True, default=None)
    raw_text = serializers.CharField(required=False, allow_null=True, default=None)


class PreferenceSerializer(serializers.Serializer):
    """Предпочтение клиента — O-1 (DRF-2816).

    ``origin`` — строкой, а не закрытым выбором: незнакомое происхождение (в т. ч.
    вывод агента) не роняет запрос, а отбрасывается в :func:`build_preferences`
    с записью в лог. Неподтверждённое не участвует — и не ломает полку.

    ``source_tenant_id`` (O-1b) — где записана память о мастере или салоне:

    * поля нет или ``"global_bot"`` — общий бот: действует во всех своих салонах;
    * id салона — действует только в нём;
    * ``null`` — происхождение неизвестно: предпочтение не едет.

    Строкой, а не UUID: незнакомое значение отбрасывает одно предпочтение,
    а не весь запрос.

    ``name`` + ``name_stems`` (DRF-2855) — мастер из памяти по ИМЕНИ: бот
    хранит имя, а не id. Ровно одно из ``ref`` / ``name``; основы даёт бот —
    морфологии в каталоге нет. Имя — только мастер и только из памяти, и
    разрешается оно только в режиме «свои салоны» (см. ручку).
    """

    kind = serializers.ChoiceField(choices=[k.value for k in PreferenceKind])
    ref = serializers.UUIDField(required=False)
    name = serializers.CharField(max_length=128, required=False)
    name_stems = serializers.ListField(
        child=serializers.CharField(min_length=NAME_STEM_MIN_LENGTH, max_length=64),
        required=False, min_length=1, max_length=MAX_NAME_STEMS,
    )
    strength = serializers.ChoiceField(
        choices=[s.value for s in PreferenceStrength], required=False, default=PreferenceStrength.SOFT.value,
    )
    origin = serializers.CharField(max_length=64)
    source_tenant_id = serializers.CharField(
        max_length=64, required=False, allow_null=True, allow_blank=True,
    )

    def validate(self, attrs: dict) -> dict:
        has_ref, has_name = "ref" in attrs, "name" in attrs
        if has_ref == has_name:
            raise serializers.ValidationError("ровно одно из ref / name")
        if not has_name:
            if "name_stems" in attrs:
                raise serializers.ValidationError("name_stems без name")
            return attrs
        if "name_stems" not in attrs:
            raise serializers.ValidationError("name требует name_stems: морфологии в каталоге нет")
        if attrs["kind"] != PreferenceKind.MASTER.value:
            raise serializers.ValidationError("по имени называется только мастер")
        if attrs["origin"] != PreferenceOrigin.CONFIRMED_MEMORY.value:
            raise serializers.ValidationError("имя разрешается только для подтверждённой памяти")
        return attrs


@dataclass(frozen=True)
class NamedMaster:
    """Мастер из памяти, названный по имени, — до разрешения (DRF-2855).

    Самого имени здесь нет намеренно: искать нужно по основам, а имя, которое
    никуда не передано, не попадёт ни в лог, ни в ответ.
    """

    stems: tuple[str, ...]
    #: Салон, где память записана; ``None`` — во всех своих салонах.
    tenant_ref: UUID | None = None
    #: ``False`` — происхождение памяти неизвестно: имя не ищется вовсе.
    origin_known: bool = True


#: Виды, у которых память — отношение клиента с одним салоном.
_SALON_BOUND_KINDS = frozenset({PreferenceKind.MASTER, PreferenceKind.SALON})

_UNSCOPED = object()


def _memory_tenant_ref(item: dict):
    """Салон памяти о мастере/салоне: ``None`` — все свои, UUID — один, ``_UNSCOPED`` — не едет."""
    if "source_tenant_id" not in item or item["source_tenant_id"] == MEMORY_SOURCE_GLOBAL_BOT:
        return None
    raw = item["source_tenant_id"]
    if raw is None:
        return _UNSCOPED
    try:
        return UUID(raw)
    except ValueError:
        return _UNSCOPED


def build_preferences(items) -> tuple[Preference, ...]:
    """Провалидированные элементы → предпочтения. Незнакомое происхождение — мимо."""
    allowed = {o.value for o in PreferenceOrigin}
    out: list[Preference] = []
    for item in items or ():
        if "name" in item:
            # Имя — не предпочтение, пока оно не разрешено: см. `named_masters`.
            continue
        if item["origin"] not in allowed:
            logger.warning(
                "recommendation.preference.dropped origin=%r kind=%s — не сказано сейчас и "
                "не подтверждено в памяти: не участвует (O-1)",
                item["origin"], item["kind"],
            )
            continue
        kind, origin = PreferenceKind(item["kind"]), PreferenceOrigin(item["origin"])
        tenant_ref = None
        if origin is PreferenceOrigin.CONFIRMED_MEMORY and kind in _SALON_BOUND_KINDS:
            tenant_ref = _memory_tenant_ref(item)
            if tenant_ref is _UNSCOPED:
                logger.warning(
                    "recommendation.preference.dropped kind=%s — память без известного салона "
                    "происхождения не участвует (O-1b)",
                    item["kind"],
                )
                continue
        out.append(Preference(
            kind=kind, ref=item["ref"], strength=PreferenceStrength(item["strength"]),
            origin=origin, tenant_ref=tenant_ref,
        ))
    return tuple(out)


def named_masters(items) -> tuple[NamedMaster, ...]:
    """Мастера, названные по имени, — ВСЕ и в порядке запроса: на каждое имя будет ответ."""
    out: list[NamedMaster] = []
    for item in items or ():
        if "name" not in item:
            continue
        tenant_ref = _memory_tenant_ref(item)
        stems = tuple(s.strip() for s in item["name_stems"])
        if tenant_ref is _UNSCOPED:
            out.append(NamedMaster(stems=stems, origin_known=False))
        else:
            out.append(NamedMaster(stems=stems, tenant_ref=tenant_ref))
    return tuple(out)


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
    # ОБЯЗАТЕЛЕН, и это не строгость ради строгости. Умолчание `UNKNOWN`
    # по контракту §14 fail-closed, то есть равносильно `STOP`: множество
    # пусто, стадии не выполняются. Забывший поле вызывающий получал бы
    # не ошибку, а **пустую выдачу** — то есть ответ «подходящих нет» на
    # вопрос, который никто не задавал. Ровно то смешение недоступности
    # с пустотой, из-за которого DEFECT-C-02 прожил незамеченным.
    #
    # Требуя поле, мы превращаем молчаливый пустой экран в 400 с именем
    # причины: состояние безопасности обязан назвать тот, кто его знает.
    safety_state = serializers.ChoiceField(choices=[s.value for s in SafetyState])
    tie_break_seed = serializers.CharField(required=False, allow_null=True, default=None)
    k = serializers.IntegerField(required=False, min_value=1, max_value=50, default=3)
    preferences = PreferenceSerializer(
        many=True, required=False, allow_null=True, max_length=MAX_PREFERENCES,
    )

    def build_preferences(self) -> tuple[Preference, ...]:
        return build_preferences(self.validated_data.get("preferences"))

    def named_masters(self) -> tuple[NamedMaster, ...]:
        return named_masters(self.validated_data.get("preferences"))

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


class _PreferenceResolutionSerializer(serializers.Serializer):
    """Чем кончилось разрешение имени. Ни имени, ни id — только исход и число."""

    kind = serializers.ChoiceField(choices=[PreferenceKind.MASTER.value])
    status = serializers.ChoiceField(choices=["resolved", "not_found", "ambiguous", "not_applicable"])
    matches = serializers.IntegerField(min_value=0)


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
    # H1-в (DRF-1934), форма — поправка O1 (§8): добавочные поля, мажор контракта прежний.
    separation_stage = serializers.ChoiceField(
        choices=[StageId.S2.value, StageId.S3.value, StageId.S4.value, StageId.S5.value], allow_null=True,
    )
    separation_state = serializers.ChoiceField(choices=[s.value for s in SeparationState])
    best_group_size = serializers.IntegerField(min_value=0)
    candidate_count = serializers.IntegerField(min_value=0)
    # O2 (§9): до калибровки по тени — всегда null.
    separation_score = serializers.FloatField(allow_null=True, min_value=0.0, max_value=1.0)
    # DRF-2855 (1.2.0): по одному элементу на КАЖДОЕ присланное имя, в порядке запроса.
    preference_resolution = _PreferenceResolutionSerializer(many=True)


def decision_to_payload(decision: RecommendationDecision, *, preference_resolution=()) -> dict:
    """Разложить решение в тело ответа. Ни строки для показа, ни балла.

    ``preference_resolution`` — исходы разрешения имён (DRF-2855); считает их
    ручка, а не резолвер: имя — дело границы, решение имён не знает.
    """
    return {
        "preference_resolution": [dict(item) for item in preference_resolution],
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
        "separation_stage": decision.separation_stage.value if decision.separation_stage else None,
        "separation_state": decision.separation_state.value,
        "best_group_size": decision.best_group_size,
        "candidate_count": decision.candidate_count,
        "separation_score": decision.separation_score,
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
