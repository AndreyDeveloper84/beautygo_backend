"""Конвейер S0–S6 — лексикографический, а не аддитивный. Контракт §3.1 R1.

Как считается порядок
---------------------
Не суммой. Кандидаты начинают одной группой; каждая следующая стадия
пытается разделить **каждую** текущую группу, и разделяет её только там,
где ей есть чем различить:

    все кандидаты
        -> S2 делит по соответствию нужде
            -> S3 делит то, что S2 не различил
                -> S4 делит то, что не различили S2 и S3
                    -> S5 делит остаток
                        -> S6 крутит внутри яруса

Получившиеся группы и есть **ярусы**. Равный `tier` означает не «примерно
одинаково», а «ни одна стадия их не различила» — и поверхность обязана
показать их как равноправных, а не назначить первого лучшим (§29.3).

Почему группы, а не кортеж ключей: кортеж пришлось бы дополнять нулями там,
где стадия для кандидата неактивна, и ноль немедленно стал бы понижением за
отсутствие данных. Группировка позволяет стадии честно промолчать.

Чего здесь нет намеренно
------------------------
* **`CandidateSetSignature` и `separation`** (§13) — интерфейс к треку A.
  `separation` определён как функция глубины различившей стадии, но её вид
  и порог `tau_separation` — `CONTROLLED_POLICY`, уже вынесенный владельцу
  (DRF-1519/DRF-1533). Написать «пока так» значило бы назначить политику
  молча — ровно то, чем стал литерал рейтинга в сиде.
* **Персистенция `Recommendation`** — авторитет домена (§6.1).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterable, Sequence
from uuid import UUID

from ._evidence import EvidenceItem
from ._reason_codes import REGISTRY_VERSION, ReasonCode, validate_candidate_codes
from ._rotation import rotate_within_tier
from ._stages import (
    StageOutput,
    StagePolicy,
    apply_eligibility,
    apply_scope,
    stage_contextual,
    stage_quality,
    stage_semantic_fit,
    stage_transaction_fit,
)
from ._types import (
    CandidateFacts,
    CandidateRef,
    CandidateSource,
    ExcludedCandidate,
    PolicyVersions,
    RankedCandidate,
    RecommendationDecision,
    RecommendationRequest,
    StageActivity,
    StageId,
    StageVerdict,
)

#: Версия настоящего контракта. Уезжает в ответ: потребитель, получивший
#: неизвестную мажорную версию, обязан вернуть CONTRACT_VIOLATION (§9.4).
RESOLVER_SPEC_VERSION = "1.0.0"

#: Ротация — чистая функция пары (seed, id); версия отдельная, потому что
#: смена ключа меняет экспозицию и обязана быть видна в истории решений.
TIE_BREAK_POLICY_VERSION = "1.0.0"

#: Порядок ранжирующих стадий. Он и есть приоритет факторов: соответствие
#: нужде раньше исполнимости, исполнимость раньше опыта, опыт раньше
#: качества. Решение владельца §29.1; менять — только вместе с ним.
_RANKING_STAGES = (StageId.S2, StageId.S3, StageId.S4, StageId.S5)


def resolve(
    request: RecommendationRequest,
    *,
    source: CandidateSource,
    policy: StagePolicy | None = None,
) -> RecommendationDecision:
    """Единственная точка, где рождается `RecommendationDecision`.

    `source` — порт к доменной правде: он отвечает, какие кандидаты есть
    в объявленном scope, и **не отвечает**, в каком они порядке. Порядок
    целиком здесь; это и есть граница между «правдой домена» и «политикой».
    """
    # Умолчание читает настройки, а не жёсткую константу: иначе одна
    # политика имеет два значения в одном процессе — у поверхности,
    # которая её собрала, и здесь. См. `StagePolicy.from_settings`.
    policy = policy or StagePolicy.from_settings()
    facts = _canonical_order(source.fetch(scope=request.scope, need=request.need))

    scoped = apply_scope(facts, request.scope)
    admitted = apply_eligibility(scoped.admitted, request, policy)

    excluded = scoped.excluded + admitted.excluded
    survivors = admitted.admitted

    stage_outputs = _run_ranking_stages(survivors, request, policy)
    tiers, verdicts = _build_tiers(survivors, stage_outputs)
    tiers = _apply_tier_one_ban(tiers, stage_outputs)
    ordered = _order_candidates(
        tiers=tiers,
        refs={f.ref.id: f.ref for f in survivors},
        seed=request.tie_break_seed,
        verdicts=verdicts,
        base_codes=(scoped.codes, admitted.codes),
        base_evidence=(admitted.evidence,),
        stage_outputs=stage_outputs,
    )

    return RecommendationDecision(
        decision_id=str(uuid.uuid4()),
        request_id=request.request_id,
        ordered=ordered,
        excluded=tuple(excluded),
        stage_activity=_stage_activity(stage_outputs),
        reason_codes=_decision_codes(ordered, excluded, stage_outputs, request),
        policy_versions=PolicyVersions(
            resolver_spec_version=RESOLVER_SPEC_VERSION,
            stage_policy_version=policy.version,
            reason_code_registry_version=REGISTRY_VERSION,
            catalog_mapping_version=(request.policy_pins.catalog_mapping_version if request.policy_pins else None),
            safety_policy_version=(request.policy_pins.safety_policy_version if request.policy_pins else None),
            tie_break_policy_version=TIE_BREAK_POLICY_VERSION,
        ),
        computed_at=datetime.now(timezone.utc),
        census=admitted.census,
    )


def _canonical_order(facts: Iterable[CandidateFacts]) -> tuple[CandidateFacts, ...]:
    """Стереть порядок источника.

    Источник отдаёт множество; если бы его порядок доживал до выдачи, он
    влиял бы на то, что увидит человек, оставаясь «просто источником» —
    то есть был бы четвёртым авторитетом, самым незаметным. Сортировка
    канонична и не является ранжированием: она ничего не знает ни о
    качестве, ни о близости, ни о рейтинге.
    """
    return tuple(sorted(facts, key=lambda f: (f.ref.kind.value, str(f.ref.id))))


def _run_ranking_stages(
    survivors: Sequence[CandidateFacts],
    request: RecommendationRequest,
    policy: StagePolicy,
) -> dict[StageId, StageOutput]:
    return {
        StageId.S2: stage_semantic_fit(survivors, request.need),
        StageId.S3: stage_transaction_fit(survivors, request, policy),
        StageId.S4: stage_contextual(survivors),
        StageId.S5: stage_quality(survivors, policy),
    }


def _build_tiers(
    survivors: Sequence[CandidateFacts],
    stage_outputs: dict[StageId, StageOutput],
) -> tuple[list[list[UUID]], dict[UUID, dict[StageId, StageVerdict]]]:
    """Разбить кандидатов на ярусы последовательным делением групп."""
    tiers: list[list[UUID]] = [[f.ref.id for f in survivors]] if survivors else []
    verdicts: dict[UUID, dict[StageId, StageVerdict]] = {
        f.ref.id: {} for f in survivors
    }

    for stage_id in _RANKING_STAGES:
        output = stage_outputs[stage_id]
        next_tiers: list[list[UUID]] = []
        for group in tiers:
            next_tiers.extend(_split_group(group, output, verdicts))
        tiers = next_tiers

    return tiers, verdicts


def _split_group(
    group: list[UUID],
    output: StageOutput,
    verdicts: dict[UUID, dict[StageId, StageVerdict]],
) -> list[list[UUID]]:
    """Разделить одну группу одной стадией — или честно промолчать.

    Стадия молчит про всю группу, если она неактивна вообще или неактивна
    **хотя бы для одного** кандидата этой группы. Второе — единственный
    непротиворечивый способ выполнить «не понижать за отсутствие данных»
    (§29.4): дать такому кандидату ключ 0 значило бы отправить его ярусом
    ниже, а объявить его равным каждому по отдельности нельзя — порядок
    перестал бы быть транзитивным.
    """
    frozen_by = output.inactive_for & set(group)
    if not output.active or frozen_by:
        for cid in group:
            verdicts[cid][output.stage] = (
                StageVerdict.INACTIVE
                if (not output.active or cid in output.inactive_for)
                else StageVerdict.TIED
            )
        return [group]

    keyed: dict[float, list[UUID]] = {}
    for cid in group:
        keyed.setdefault(output.keys.get(cid, 0.0), []).append(cid)

    distinguished = len(keyed) > 1
    for cid in group:
        verdicts[cid][output.stage] = (
            StageVerdict.DISTINGUISHED if distinguished else StageVerdict.TIED
        )
    return [keyed[key] for key in sorted(keyed, reverse=True)]


def _apply_tier_one_ban(
    tiers: list[list[UUID]],
    stage_outputs: dict[StageId, StageOutput],
) -> list[list[UUID]]:
    """K5: кандидату с неподтверждённым расписанием первый ярус запрещён.

    Когда подтверждённых нет вовсе, верхняя группа состоит только из
    запрещённых — и тогда первый ярус остаётся **пустым**. Это не отказ
    выдавать: кандидаты выдаются, нумерация ярусов просто начинается со
    второго. Смысл ровно тот, которого требует решение владельца §29.5 —
    сказать «первого нет», а не назначить первым того, чьё расписание никто
    не подтверждал.
    """
    banned = stage_outputs[StageId.S3].tier_one_forbidden
    if not banned or not tiers:
        return tiers
    if all(cid in banned for cid in tiers[0]):
        return [[]] + tiers
    return tiers


def _order_candidates(
    *,
    tiers: list[list[UUID]],
    refs: dict[UUID, CandidateRef],
    seed: str | None,
    verdicts: dict[UUID, dict[StageId, StageVerdict]],
    base_codes: tuple[dict[UUID, frozenset[ReasonCode]], ...],
    base_evidence: tuple[dict[UUID, tuple[EvidenceItem, ...]], ...],
    stage_outputs: dict[StageId, StageOutput],
) -> tuple[RankedCandidate, ...]:
    """Собрать выдачу: ротация внутри яруса, затем сквозная нумерация."""
    ordered: list[RankedCandidate] = []
    rank = 1
    for tier_index, group in enumerate(tiers, start=1):
        rotated = rotate_within_tier(group, seed)
        shared = len(group) > 1
        for cid in rotated:
            codes: set[ReasonCode] = set()
            evidence: list[EvidenceItem] = []
            for bucket in base_codes:
                codes |= bucket.get(cid, frozenset())
            for bucket in base_evidence:
                evidence.extend(bucket.get(cid, ()))
            for output in stage_outputs.values():
                codes |= output.codes.get(cid, frozenset())
                evidence.extend(output.evidence.get(cid, ()))
            if shared:
                codes.add(ReasonCode.TIE_TIER_SHARED)
                if seed is not None:
                    codes.add(ReasonCode.TIE_ROTATION_APPLIED)

            ordered.append(
                RankedCandidate(
                    candidate_ref=refs[cid],
                    rank=rank,
                    tier=tier_index,
                    reason_codes=validate_candidate_codes(frozenset(codes)),
                    evidence=tuple(evidence),
                    stage_verdicts=dict(verdicts.get(cid, {})),
                )
            )
            rank += 1
    return tuple(ordered)


def _stage_activity(stage_outputs: dict[StageId, StageOutput]) -> tuple[StageActivity, ...]:
    """`ACTIVE | INACTIVE(reason)` — чтобы «почему не различили» читалось без кода."""
    rows = [
        StageActivity(StageId.S0, True),
        StageActivity(StageId.S1, True),
    ]
    for stage_id in _RANKING_STAGES:
        output = stage_outputs[stage_id]
        rows.append(StageActivity(stage_id, output.active, output.inactive_reason))
    rows.append(StageActivity(StageId.S6, True))
    return tuple(rows)


def _decision_codes(
    ordered: Sequence[RankedCandidate],
    excluded: Sequence[ExcludedCandidate],
    stage_outputs: dict[StageId, StageOutput],
    request: RecommendationRequest,
) -> tuple[ReasonCode, ...]:
    """Коды про решение В ЦЕЛОМ, а не про кандидата."""
    codes: set[ReasonCode] = set()
    if not stage_outputs[StageId.S5].active:
        codes.add(ReasonCode.QUALITY_NO_EVIDENCE)
    if not ordered and any(e.reason_code is ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE for e in excluded):
        # Каталог видно, рекомендовать нечего — сигнал треку A
        # BLOCKED(CATALOG_NOT_RECOMMENDABLE), §10.3.
        codes.add(ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE)
    if not ordered and any(e.reason_code is ReasonCode.ELIG_EXCLUDED_SAFETY for e in excluded):
        codes.add(ReasonCode.ELIG_EXCLUDED_SAFETY)
    return tuple(sorted(codes))
