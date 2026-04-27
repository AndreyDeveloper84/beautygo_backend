"""Unit tests for RecommendationEngine — DRF-105."""
from __future__ import annotations

from decimal import Decimal

import pytest

from ai.application.services.recommendation_engine import (
    RecommendationEngine,
    RecommendationQuery,
    ScoreBreakdown,
    WEIGHT_AVAILABILITY,
    WEIGHT_DISTANCE,
    WEIGHT_HISTORY,
    WEIGHT_RATING,
    WEIGHT_SERVICE_MATCH,
)
from ai.tests.factories import make_specialist, make_user
from services.models import Service, ServiceCategory


pytestmark = pytest.mark.django_db


def _make_service(specialist, *, name="Test Service", price="1500", category=None):
    return Service.objects.create(
        specialist=specialist,
        name=name,
        price=Decimal(price),
        duration_minutes=60,
        is_active=True,
        category=category,
    )


# ---------------------------------------------------------------------------
# Score weights contract
# ---------------------------------------------------------------------------


class TestWeights:
    def test_weights_sum_to_one(self):
        total = (
            WEIGHT_RATING + WEIGHT_DISTANCE + WEIGHT_AVAILABILITY
            + WEIGHT_SERVICE_MATCH + WEIGHT_HISTORY
        )
        assert abs(total - 1.0) < 1e-9

    def test_weights_match_drf_105_contract(self):
        """Spec: 30/25/20/15/10. Locked here so a stealth weight tweak
        breaks the test instead of changing recommendations silently."""
        assert WEIGHT_RATING == 0.30
        assert WEIGHT_DISTANCE == 0.25
        assert WEIGHT_AVAILABILITY == 0.20
        assert WEIGHT_SERVICE_MATCH == 0.15
        assert WEIGHT_HISTORY == 0.10


# ---------------------------------------------------------------------------
# Sub-scorers
# ---------------------------------------------------------------------------


class TestRatingScore:
    def test_high_rating_high_reviews_scores_near_one(self):
        s = make_specialist(rating=4.9, reviews_count=200)
        assert RecommendationEngine._score_rating(s) > 0.9

    def test_high_rating_few_reviews_dampened(self):
        """5★ with 2 reviews shouldn't beat 4.7★ with 80 reviews."""
        fresh = make_specialist(rating=5.0, reviews_count=2)
        established = make_specialist(rating=4.7, reviews_count=80)
        assert (
            RecommendationEngine._score_rating(fresh)
            < RecommendationEngine._score_rating(established)
        )

    def test_one_star_zero_reviews_zero(self):
        s = make_specialist(rating=1.0, reviews_count=0)
        assert RecommendationEngine._score_rating(s) == 0.0


class TestDistanceScore:
    def test_zero_distance_one(self):
        engine = RecommendationEngine()
        assert engine._score_distance(0.0) == 1.0

    def test_max_distance_zero(self):
        engine = RecommendationEngine(max_distance_km=20.0)
        assert engine._score_distance(20.0) == 0.0
        assert engine._score_distance(25.0) == 0.0  # beyond max also 0

    def test_half_distance_half_score(self):
        engine = RecommendationEngine(max_distance_km=20.0)
        assert engine._score_distance(10.0) == pytest.approx(0.5)

    def test_none_distance_neutral_half(self):
        engine = RecommendationEngine()
        assert engine._score_distance(None) == 0.5


class TestServiceMatchScore:
    def test_no_filters_returns_one(self, db):
        s = make_specialist()
        q = RecommendationQuery()
        assert RecommendationEngine._score_service_match(s, q) == 1.0

    def test_category_match_full_score(self, db):
        cat = ServiceCategory.objects.create(name="Маникюр")
        s = make_specialist()
        _make_service(s, category=cat, price="1000")
        q = RecommendationQuery(category_id=cat.id)
        assert RecommendationEngine._score_service_match(s, q) == 1.0

    def test_category_no_match_zero(self, db):
        cat_a = ServiceCategory.objects.create(name="Маникюр")
        cat_b = ServiceCategory.objects.create(name="Стрижка")
        s = make_specialist()
        _make_service(s, category=cat_a, price="1000")
        q = RecommendationQuery(category_id=cat_b.id)
        assert RecommendationEngine._score_service_match(s, q) == 0.0

    def test_category_match_but_price_too_high(self, db):
        cat = ServiceCategory.objects.create(name="Маникюр")
        s = make_specialist()
        _make_service(s, category=cat, price="3000")
        q = RecommendationQuery(category_id=cat.id, price_max=Decimal("1500"))
        # In category but above price ceiling — partial credit (0.5)
        score = RecommendationEngine._score_service_match(s, q)
        assert 0.0 < score < 1.0


class TestHistoryScore:
    def test_first_time_client_zero(self):
        s = make_specialist()
        score = RecommendationEngine._score_history(s, set(), set())
        assert score == 0.0

    def test_returning_to_same_specialist_full(self):
        s = make_specialist()
        score = RecommendationEngine._score_history(s, {s.id}, set())
        assert score == 1.0

    def test_same_category_history_half(self, db):
        cat = ServiceCategory.objects.create(name="Маникюр")
        s = make_specialist()
        _make_service(s, category=cat)
        # Client never used this specialist but used the category before.
        score = RecommendationEngine._score_history(s, set(), {cat.id})
        assert score == 0.5


# ---------------------------------------------------------------------------
# End-to-end recommend()
# ---------------------------------------------------------------------------


class TestRecommendEnd2End:
    def test_returns_active_high_rating_specialists_first(self, db):
        a = make_specialist(display_name="Анна", rating=4.9, reviews_count=80)
        b = make_specialist(display_name="Борис", rating=4.5, reviews_count=20)
        make_specialist(display_name="Виктория", rating=4.0, reviews_count=5)
        engine = RecommendationEngine()
        result = engine.recommend(
            RecommendationQuery(min_rating=4.0, limit=10),
            use_cache=False,
        )
        ids = [s.id for s in result.candidates]
        assert a.id in ids
        assert b.id in ids
        # Анна должна быть первой — самый высокий рейтинг + reviews
        assert result.candidates[0].id == a.id

    def test_filters_by_min_rating(self, db):
        a = make_specialist(display_name="High", rating=4.9, reviews_count=50)
        make_specialist(display_name="Mid", rating=4.0, reviews_count=20)
        engine = RecommendationEngine()
        result = engine.recommend(
            RecommendationQuery(min_rating=4.5, limit=10),
            use_cache=False,
        )
        ids = [s.id for s in result.candidates]
        assert a.id in ids
        assert len(ids) == 1

    def test_distance_affects_ranking(self, db):
        from decimal import Decimal as D

        # Both 5★ — distance breaks tie.
        near = make_specialist(display_name="Near", rating=4.9, reviews_count=50)
        far = make_specialist(display_name="Far", rating=4.9, reviews_count=50)

        # Penza coords
        near.location_lat = D("53.2007")
        near.location_lng = D("45.0046")
        near.save()
        # ~30km from Penza
        far.location_lat = D("53.5")
        far.location_lng = D("45.0046")
        far.save()

        engine = RecommendationEngine(max_distance_km=50.0)
        result = engine.recommend(
            RecommendationQuery(
                client_lat=53.2007,
                client_lon=45.0046,
                limit=10,
            ),
            use_cache=False,
        )
        ids = [s.id for s in result.candidates]
        # Near master should rank first.
        assert ids[0] == near.id

    def test_history_boosts_familiar_specialist(self, db):
        from datetime import datetime, timezone as dt_tz
        from appointments.models import Appointment

        client_user = make_user(role="client")
        familiar = make_specialist(
            display_name="Familiar", rating=4.5, reviews_count=20,
        )
        higher_rated = make_specialist(
            display_name="HigherRated", rating=4.9, reviews_count=50,
        )

        # Client previously COMPLETED an appointment with `familiar`.
        cat = ServiceCategory.objects.create(name="Маникюр")
        svc = _make_service(familiar, category=cat)
        Appointment.objects.create(
            client=client_user,
            specialist=familiar,
            service=svc,
            start_datetime=datetime(2026, 4, 1, 10, 0, tzinfo=dt_tz.utc),
            end_datetime=datetime(2026, 4, 1, 11, 0, tzinfo=dt_tz.utc),
            status=Appointment.Status.COMPLETED,
            price=svc.price,
            snapshot_price=svc.price,
            snapshot_service_name=svc.name,
            snapshot_duration_minutes=60,
        )

        engine = RecommendationEngine()
        result = engine.recommend(
            RecommendationQuery(client_id=client_user.id, limit=10),
            use_cache=False,
        )
        # History (10%) should boost familiar enough that even a higher-rated
        # competitor gets edged in some configurations. We verify the
        # familiar specialist's score breakdown shows history component
        # firing, which is the contractual property here.
        familiar_scored = next(s for s in result.candidates if s.id == familiar.id)
        assert familiar_scored.breakdown.history == 1.0
        higher_scored = next(s for s in result.candidates if s.id == higher_rated.id)
        assert higher_scored.breakdown.history == 0.0


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


class TestCaching:
    def test_second_call_hits_cache(self, db, settings):
        from django.core.cache import cache as default_cache

        default_cache.clear()
        make_specialist(rating=4.9, reviews_count=50)

        engine = RecommendationEngine()
        q = RecommendationQuery(limit=5)

        first = engine.recommend(q)
        # Spy on the cache: second call should not recompute.
        # Verify by mutating a specialist between calls and confirming
        # the cached result still shows old state.
        new_master = make_specialist(rating=5.0, reviews_count=200)
        second = engine.recommend(q)
        assert {s.id for s in first.candidates} == {s.id for s in second.candidates}
        assert new_master.id not in {s.id for s in second.candidates}

    def test_use_cache_false_bypasses(self, db):
        from django.core.cache import cache as default_cache

        default_cache.clear()
        make_specialist(rating=4.9, reviews_count=50)
        engine = RecommendationEngine()
        q = RecommendationQuery(limit=5)
        engine.recommend(q)  # warm cache

        new_master = make_specialist(rating=5.0, reviews_count=200)
        second = engine.recommend(q, use_cache=False)
        assert new_master.id in {s.id for s in second.candidates}

    def test_cache_keys_differ_by_filter(self):
        q1 = RecommendationQuery(client_lat=53.0, client_lon=45.0)
        q2 = RecommendationQuery(client_lat=55.0, client_lon=37.0)
        assert q1.cache_key() != q2.cache_key()

    def test_cache_keys_match_for_same_filter(self):
        q1 = RecommendationQuery(client_lat=53.0001, client_lon=45.0001)
        q2 = RecommendationQuery(client_lat=53.0002, client_lon=45.0002)
        # Coordinates rounded to 3 decimals → same key (small movement
        # shouldn't bust the cache).
        assert q1.cache_key() == q2.cache_key()


# ---------------------------------------------------------------------------
# Score breakdown surfacing
# ---------------------------------------------------------------------------


class TestScoreBreakdown:
    def test_top_reasons_picks_largest_contributors(self):
        # Distance is dominant.
        b = ScoreBreakdown(
            rating=0.5, distance=1.0, availability=1.0,
            service_match=0.5, history=0.0,
        )
        reasons = b.top_reasons()
        assert reasons[0] == "Близко"

    def test_top_reasons_skips_low_contribution(self):
        b = ScoreBreakdown(
            rating=0.0, distance=0.0, availability=0.0,
            service_match=0.0, history=0.0,
        )
        # All zero — no reasons surface.
        assert b.top_reasons() == []

    def test_composite_in_zero_one_range(self):
        b = ScoreBreakdown(
            rating=1.0, distance=1.0, availability=1.0,
            service_match=1.0, history=1.0,
        )
        assert b.composite == pytest.approx(1.0)
        z = ScoreBreakdown(0.0, 0.0, 0.0, 0.0, 0.0)
        assert z.composite == 0.0
