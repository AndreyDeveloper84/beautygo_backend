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
from users.models import SpecialistProfile


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


# ---------------------------------------------------------------------------
# DRF-1433 — «оценок нет» ≠ «оценки плохие»
# ---------------------------------------------------------------------------


class TestUnratedSpecialistIsNotCutOff:
    """Порог отсекает плохие оценки, а не их отсутствие.

    Замер боевого пилота 31.08: девять мастеров, у всех ``rating=0.0``
    и ``reviews_count=0``; подбор с порогом по умолчанию
    (``AI_SPECIALIST_MIN_RATING=4.0``) возвращал 0 кандидатов, с
    ``min_rating=0`` — 9. Рейтинг берётся из отзывов, отзыв — после
    визита, визит — после записи, запись — после подбора: новый мастер
    из нуля выйти не мог.

    Отличить одно состояние от другого позволяет ``reviews_count``: он
    пересчитывается в ``reviews.views._recalculate_rating`` как
    ``Count`` неспрятанных отзывов, то есть ``reviews_count == 0``
    означает ровно «оценок нет».

    Каждое отрицательное утверждение здесь идёт в паре с положительным
    на тех же данных: иначе «никого лишнего не отсекли» было бы
    неотличимо от «сняли порог совсем».
    """

    def test_result_exposes_candidates_and_has_no_items_field(self, db):
        """Страж имени поля.

        У ``RecommendationResult`` есть ``candidates`` и нет ``items``:
        чтение ``getattr(result, "items", [])`` дало бы ложный ноль на
        любой выдаче и читалось бы как «подбор никого не вернул».
        """
        make_specialist(display_name="Есть", rating=4.9, reviews_count=40)
        result = RecommendationEngine().recommend(
            RecommendationQuery(limit=10), use_cache=False,
        )
        assert hasattr(result, "candidates")
        assert not hasattr(result, "items")
        assert len(result.candidates) == 1

    def test_specialist_without_reviews_is_in_output(self, db):
        """Положительная сторона: мастер без отзывов попадает в выдачу."""
        newcomer = make_specialist(
            display_name="Новичок", rating=0.0, reviews_count=0,
        )
        result = RecommendationEngine().recommend(
            RecommendationQuery(limit=10), use_cache=False,
        )
        assert [s.id for s in result.candidates] == [newcomer.id]

    def test_specialist_with_bad_reviews_is_not_in_output(self, db):
        """Отрицательная сторона на тех же данных: порог продолжает
        работать против ПЛОХИХ оценок.

        Без этой пары «починка» неотличима от снятия порога вовсе.
        """
        newcomer = make_specialist(
            display_name="Новичок", rating=0.0, reviews_count=0,
        )
        good = make_specialist(
            display_name="Хороший", rating=4.8, reviews_count=30,
        )
        bad = make_specialist(
            display_name="Плохой", rating=2.0, reviews_count=12,
        )
        result = RecommendationEngine().recommend(
            RecommendationQuery(limit=10), use_cache=False,
        )
        ids = [s.id for s in result.candidates]
        assert newcomer.id in ids
        assert good.id in ids
        assert bad.id not in ids

    def test_newcomer_ranks_below_well_reviewed_all_else_equal(self, db):
        """Порядок выдачи: новичок не обгоняет хорошего мастера просто
        потому, что перестал отсекаться.

        Всё прочее одинаково (гео нет, услуг нет, истории нет), поэтому
        различает их только компонент рейтинга: ``reviews_count=0``
        обнуляет насыщение и весь его 30%-й вклад.
        """
        newcomer = make_specialist(
            display_name="Новичок", rating=0.0, reviews_count=0,
        )
        good = make_specialist(
            display_name="Хороший", rating=4.8, reviews_count=30,
        )
        result = RecommendationEngine().recommend(
            RecommendationQuery(limit=10), use_cache=False,
        )
        ids = [s.id for s in result.candidates]
        assert ids == [good.id, newcomer.id]

    def test_newcomer_is_never_sold_as_highly_rated(self, db):
        """Правдивость причин: новичок попал в выдачу, но «Высокий
        рейтинг» ему приписать нельзя — оценок нет.

        Пара к предыдущему тесту и страж против соблазна выдать
        новичку стартовый рейтинг: владелец этот вариант отверг
        именно потому, что он показывает клиенту выдуманную оценку
        как настоящую (31.08).
        """
        newcomer = make_specialist(
            display_name="Новичок", rating=0.0, reviews_count=0,
        )
        good = make_specialist(
            display_name="Хороший", rating=4.8, reviews_count=30,
        )
        result = RecommendationEngine().recommend(
            RecommendationQuery(limit=10), use_cache=False,
        )
        by_id = {s.id: s for s in result.candidates}
        assert by_id[newcomer.id].breakdown.rating == 0.0
        assert "Высокий рейтинг" not in by_id[newcomer.id].match_reasons
        # Положительная сторона на тех же данных: у мастера с отзывами
        # причина «Высокий рейтинг» есть — значит тест выше проверяет
        # отсутствие, а не сломанный расчёт причин.
        assert by_id[good.id].breakdown.rating > 0.0
        assert "Высокий рейтинг" in by_id[good.id].match_reasons

    def test_newcomer_survives_prefetch_slice_on_a_full_catalog(self, db):
        """Порог снят — но выборку до скоринга режет ``[: limit * 3]``
        с сортировкой по ``-rating``, где мастер без оценок стоит
        ПОСЛЕДНИМ.

        На пилоте (9 мастеров) это незаметно, а на каталоге больше
        ``limit * 3`` гарантия молча исчезает: новичок не доходит до
        скоринга вовсе. Здесь 7 мастеров с оценками при ``limit=2``
        (headroom 6) — ровно тот случай.

        Новичок выигрывает по расстоянию: он в точке клиента, все
        остальные дальше 25 км (``DEFAULT_MAX_DISTANCE_KM``). Дойди он
        до скоринга — он первый.
        """
        from decimal import Decimal as D

        client_lat, client_lon = 53.2007, 45.0046
        for i in range(7):
            far = make_specialist(
                display_name=f"Дальний {i}", rating=4.0, reviews_count=100,
            )
            SpecialistProfile.objects.filter(id=far.id).update(
                location_lat=D("54.0"), location_lng=D("45.0046"),
            )
        newcomer = make_specialist(
            display_name="Новичок", rating=0.0, reviews_count=0,
        )
        SpecialistProfile.objects.filter(id=newcomer.id).update(
            location_lat=D(str(client_lat)), location_lng=D(str(client_lon)),
        )

        result = RecommendationEngine().recommend(
            RecommendationQuery(
                client_lat=client_lat, client_lon=client_lon, limit=2,
            ),
            use_cache=False,
        )
        assert result.candidates[0].id == newcomer.id
