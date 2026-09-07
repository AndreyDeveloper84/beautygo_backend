"""Фабрики фактов для тестов резолвера. Базы данных здесь нет и не нужно.

Резолвер — чистая функция от `RecommendationRequest` и множества фактов
(`CandidateSource`). Это не удобство тестирования, а свойство границы:
именно оно позволит вынести модуль в `ayla-ai-core` после пилота, не
переписывая ни одной стадии. Тесты, которым понадобилась бы база, означали
бы, что резолвер сам ходит за правдой — то есть границы нет.
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Sequence

import pytest

from recommendation.api import (
    CandidateFacts,
    CandidateKind,
    CandidateRef,
    MappingStatus,
    MatchLevel,
    NeedOrigin,
    NeedSpec,
    RatingValue,
    RecommendationRequest,
    SafetyState,
    Scope,
    ScopeMode,
    Surface,
)


#: «Аргумент не передавали» — не то же самое, что «передали None».
#: Тесту про K2 нужно сказать именно «услуга НЕ разрешена», и без часового
#: это не отличить от умолчания фабрики.
_DEFAULT = object()


def make_facts(
    *,
    cid: uuid.UUID | None = None,
    kind: CandidateKind = CandidateKind.PROVIDER,
    tenant_ref: uuid.UUID | None = None,
    city: str | None = "Москва",
    distance_km: float | None = None,
    match_level: MatchLevel = MatchLevel.SERVICE_EXACT,
    matched_service_ref=_DEFAULT,
    rating: tuple[str, int] | None = None,
    prior_completed_visit: bool = False,
    is_bookable: bool | None = None,
    mapping_status: MappingStatus = MappingStatus.VERIFIED,
    price: Decimal | None = None,
    **overrides,
) -> CandidateFacts:
    """Кандидат, по умолчанию допустимый: активен, способен, маппинг VERIFIED.

    Умолчания намеренно «хорошие» — тест про исключение обязан испортить
    ровно то поле, про которое он написан, и это видно в его теле.
    """
    return CandidateFacts(
        ref=CandidateRef(kind, cid or uuid.uuid4()),
        tenant_ref=tenant_ref,
        city=city,
        distance_km=distance_km,
        is_active_offer=True,
        is_capable=True,
        mapping_status=mapping_status,
        price=price,
        match_level=match_level,
        matched_service_ref=uuid.uuid4() if matched_service_ref is _DEFAULT else matched_service_ref,
        is_bookable=is_bookable,
        prior_completed_visit=prior_completed_visit,
        rating=RatingValue(Decimal(rating[0]), rating[1]) if rating else None,
        **overrides,
    )


def make_request(
    *,
    need: NeedSpec | None = None,
    scope: Scope | None = None,
    seed: str | None = "conversation-1",
    surface: Surface = Surface.MINIAPP_HOME,
    safety_state: SafetyState = SafetyState.NORMAL,
    **overrides,
) -> RecommendationRequest:
    return RecommendationRequest(
        request_id="req-1",
        subject_ref="subject-1",
        surface=surface,
        scope=scope or Scope(ScopeMode.MARKETPLACE),
        need=need or NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text="массаж"),
        safety_state=safety_state,
        tie_break_seed=seed,
        **overrides,
    )


class StaticSource:
    """`CandidateSource`, отдающий заранее заданное множество.

    Порядок, в котором он отдаёт кандидатов, специально произвольный:
    тест `test_source_order_does_not_reach_output` доказывает, что он
    до выдачи не доживает.
    """

    def __init__(self, candidates: Sequence[CandidateFacts]) -> None:
        self._candidates = list(candidates)

    def fetch(self, *, scope, need) -> Sequence[CandidateFacts]:  # noqa: ARG002 - порт шире, чем нужно заглушке
        return list(self._candidates)


@pytest.fixture
def source_factory():
    return StaticSource
