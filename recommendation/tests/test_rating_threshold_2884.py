"""DRF-2884 (O-2) — рейтинг ранжирует после порога: 5 подтверждённых отзывов.

Решение владельца 07.10.2026 (лист решений, п.27):

- минимум — 5 отзывов; ниже порога оценка в ранжировании не участвует;
- если среди кандидатов, равных по предыдущим критериям, у кого-то отзывов
  недостаточно — рейтинг не переставляет ВСЮ эту группу;
- сравнивается обычная средняя оценка, без коэффициента числа отзывов;
- при одинаковой оценке порядок задают остальные правила;
- импортированные оценки в ранжирование не идут — сегодня потому, что у них
  ноль отзывов Ayla (поля происхождения оценки нет).

Всё здесь идёт через ПОЛИТИКУ ПО УМОЛЧАНИЮ — ту, что получает ручка
`resolve`: порог не передаётся узлом, он должен быть назначен в коде.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from recommendation import _evidence
from recommendation._evidence import EvidenceStrength, RatingValue, rating_strength
from recommendation._pipeline import resolve
from recommendation._reason_codes import ReasonCode
from recommendation._stages import StagePolicy
from recommendation._types import SeparationState, StageId
from recommendation.tests.conftest import StaticSource, make_facts, make_request


def _decision(*ratings):
    """Решение на политике по умолчанию; кандидаты в том же порядке, что оценки."""
    facts = [make_facts(rating=rating) for rating in ratings]
    decision = resolve(make_request(k=10), source=StaticSource(facts))
    tiers = {c.candidate_ref.id: c.tier for c in decision.ordered}
    codes = {c.candidate_ref.id: set(c.reason_codes) for c in decision.ordered}
    return decision, [tiers[f.ref.id] for f in facts], [codes[f.ref.id] for f in facts]


class TestTheThresholdIsFive:
    def test_the_owner_named_five(self):
        assert _evidence.N_SUBSTANTIATED == 5

    @pytest.mark.parametrize(
        ("reviews", "expected"),
        [
            (0, EvidenceStrength.UNSUBSTANTIATED),
            (1, EvidenceStrength.WEAK),
            (4, EvidenceStrength.WEAK),
            (5, EvidenceStrength.CONFIRMED),
            (500, EvidenceStrength.CONFIRMED),
        ],
    )
    def test_the_boundary_is_at_five_reviews(self, reviews, expected):
        assert rating_strength(RatingValue(Decimal("4.8"), reviews)) is expected

    def test_the_default_policy_carries_the_threshold_to_the_stage(self):
        """Порог назначен там, где его читает стадия: политика из настроек его не теряет."""
        _, tiers, codes = _decision(("4.9", 5), ("4.1", 5))

        assert tiers == [1, 2]
        assert all(ReasonCode.QUALITY_RATING_SUBSTANTIATED in c for c in codes)
        assert StagePolicy.from_settings().n_substantiated is None, "своего порога у политики нет — берётся общий"


class TestRatingRanksOnlyAConfirmedGroup:
    def test_confirmed_ratings_order_the_group(self):
        decision, tiers, _ = _decision(("4.2", 7), ("4.9", 5), ("4.6", 30))

        assert tiers == [3, 1, 2]
        assert decision.separation_stage is StageId.S5
        assert decision.separation_state is SeparationState.SPLIT

    @pytest.mark.parametrize(
        "short",
        [
            pytest.param(("5.0", 4), id="four-reviews"),
            pytest.param(("5.0", 0), id="a-rating-with-no-reviews"),
            pytest.param(None, id="no-rating-at-all"),
        ],
    )
    def test_one_candidate_short_of_reviews_leaves_the_whole_group_as_it_was(self, short):
        """Владелец: рейтинг не переставляет всю группу — и низкая подтверждённая оценка вниз не уходит."""
        decision, tiers, _ = _decision(("4.9", 50), ("3.0", 50), short)

        assert tiers == [1, 1, 1]
        assert decision.separation_state is SeparationState.NOT_SPLIT

    def test_positive_control_the_same_group_without_the_short_one_is_ordered(self):
        _, tiers, _ = _decision(("4.9", 50), ("3.0", 50))

        assert tiers == [1, 2]


class TestThePlainAverageIsCompared:
    def test_more_reviews_do_not_outweigh_a_higher_rating(self):
        """Без коэффициента числа отзывов: 4.81 при 10 отзывах выше 4.80 при 300."""
        _, tiers, _ = _decision(("4.81", 10), ("4.80", 300))

        assert tiers == [1, 2]

    def test_equal_ratings_stay_in_one_tier_whatever_the_review_counts(self):
        """При одинаковой оценке порядок задают остальные правила, а не число отзывов."""
        decision, tiers, _ = _decision(("4.8", 5), ("4.8", 500), ("4.8", 50))

        assert tiers == [1, 1, 1]
        assert decision.separation_state is SeparationState.NOT_SPLIT


class TestAnImportedRatingDoesNotRank:
    def test_a_rating_with_no_ayla_reviews_is_carried_but_ignored(self):
        """Случай пилота (замер 07.10): оценка 4.x при нуле отзывов. Число едет справочно, с кодом
        «учтено не было», и на порядок не влияет."""
        decision, tiers, codes = _decision(("4.9", 0), ("4.4", 0))

        assert tiers == [1, 1]
        assert all(ReasonCode.QUALITY_RATING_UNSUBSTANTIATED_IGNORED in c for c in codes)
        assert not any(ReasonCode.QUALITY_RATING_SUBSTANTIATED in c for c in codes)
        assert decision.separation_state is SeparationState.NOT_SPLIT

    def test_it_does_not_outrank_a_confirmed_one_and_is_not_pushed_below_it(self):
        _, tiers, _ = _decision(("5.0", 0), ("4.0", 20))

        assert tiers == [1, 1]
