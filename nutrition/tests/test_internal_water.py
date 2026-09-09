"""Integration tests for /api/v1/nutrition/internal/water/* (DRF-302).

Covers the WaterEntry create/delete/restore/today flows including the
acceptance criteria from docs/plans/maxbot-phase3-linear-issues.md:
milestone idempotency per-day per-threshold, alcohol hint, caffeine
warning under pregnancy, eating-disorder mode field stripping, soft-
delete + 15-minute restore window, ml validation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz
from unittest.mock import patch

import pytest
from django.core.management import call_command
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import FoodLog, WaterEntry
from nutrition.services.water_entry_service import (
    NutritionContext,
    purge_deleted_water_entries,
)
from users.models import User


pytestmark = pytest.mark.django_db


SERVICE_TOKEN = "test-token-DRF-302"
WATER_URL = "/api/v1/nutrition/internal/water/"
TODAY_URL = "/api/v1/nutrition/internal/water/today/"


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def proxy_user(db):
    """Человек С анкетой питания — и всё равно БЕЗ ориентира.

    Дорога сюда была двухступенчатой, и помнить её надо целиком:

    1. Норма приезжала из ``settings.NUTRITION_DEFAULT_WATER_GOAL_ML``
       и была у всех, включая тех, кто анкету не проходил: 2000 мл при
       стакане 250 — ровно та «восьмёрка», которую из клиента уже
       выбрасывали. Её сняли, и норма стала читаться из анкеты.
    2. Читалась она из ``NutritionProfile.daily_water_ml``, а туда её
       клала формула ``30 мл × вес`` (+300/+700). Владелец снял и
       формулу (§82), так что «своё число из анкеты» оказалось тем же
       чужим числом с лишним шагом.

    Профиль в фикстуре оставлен со СТАРЫМ значением 2000 нарочно: это
    самое невыгодное предусловие для правки. У существующих клиентов
    столбец так и остался заполненным (миграция — отдельный срез), и
    тесты ниже доказывают, что наружу оно всё равно не уезжает.
    """
    from nutrition.models import NutritionProfile

    user = User.objects.create(
        username="bot:302", role="client", is_proxy=True,
    )
    NutritionProfile.objects.create(user=user, daily_water_ml=2000)
    return user


@pytest.fixture
def seed(db):
    call_command("seed_beverages")


@pytest.fixture
def headers():
    return {
        "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
        "HTTP_X_EXTERNAL_USER_ID": "bot:302",
    }


def _post_water(c: APIClient, body: dict, headers: dict, idem: str | None = None):
    h = dict(headers)
    if idem:
        h["HTTP_IDEMPOTENCY_KEY"] = idem
    return c.post(WATER_URL, body, format="json", **h)


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


class TestAuthAndValidation:
    def test_missing_service_token_returns_401(self, proxy_user):
        c = APIClient()
        resp = c.post(
            WATER_URL,
            {"ml": 250},
            format="json",
            HTTP_X_EXTERNAL_USER_ID="bot:302",
        )
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_ml_below_min_rejected(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 5}, headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_ml_above_max_rejected(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 5000}, headers)
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    def test_unknown_beverage_rejected(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(
            c, {"ml": 250, "beverage_slug": "doesnotexist"}, headers,
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST


# ---------------------------------------------------------------------------
# Basic create
# ---------------------------------------------------------------------------


class TestCreateWater:
    def test_pure_water_no_beverage(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 250}, headers)
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        body = resp.json()["data"]
        assert body["water_ml"] == 250
        assert body["kcal"] == 0
        assert body["beverage_name"] is None
        assert body["today_total_water_ml"] == 250
        # Ориентира и процента в ответе НЕТ. Формула 30 мл × вес снята
        # до утверждения методики (§82, §85 раздел 4), а процент — её
        # производная: доли от несуществующей нормы не бывает.
        assert "today_norm_water_ml" not in body
        assert "today_progress_pct" not in body

    def test_coffee_applies_macros(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(
            c, {"ml": 200, "beverage_slug": "kofe_chernyi"}, headers,
        )
        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        body = resp.json()["data"]
        assert body["beverage_name"] == "Чёрный кофе"
        assert body["beverage_label"] == "чашка"
        assert body["water_ml"] == 200  # water_coefficient 1.0
        assert body["caffeine_mg"] == 80.0  # 200ml × 40 mg/100ml

    def test_kcal_creates_food_log_mirror(self, proxy_user, seed, headers):
        """Beverages with kcal>0 mirror into FoodLog so /summary/ stays accurate."""
        c = APIClient()
        resp = _post_water(c, {"ml": 200, "beverage_slug": "latte"}, headers)
        assert resp.status_code == status.HTTP_201_CREATED
        entry = WaterEntry.objects.get(id=resp.json()["data"]["entry_id"])
        assert entry.food_log_id is not None
        assert FoodLog.objects.filter(id=entry.food_log_id).exists()

    def test_zero_kcal_skips_food_log_mirror(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 250}, headers)
        entry = WaterEntry.objects.get(id=resp.json()["data"]["entry_id"])
        assert entry.food_log_id is None


# ---------------------------------------------------------------------------
# Idempotency-Key
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_replay_returns_same_entry(self, proxy_user, seed, headers):
        c = APIClient()
        body = {"ml": 250}
        r1 = _post_water(c, body, headers, idem="abc-123")
        r2 = _post_water(c, body, headers, idem="abc-123")
        assert r1.status_code == r2.status_code == status.HTTP_201_CREATED
        assert r1.json()["data"]["entry_id"] == r2.json()["data"]["entry_id"]
        assert WaterEntry.objects.count() == 1


# ---------------------------------------------------------------------------
# Milestone idempotency (per-day per-threshold)
# ---------------------------------------------------------------------------


class TestMilestoneIdempotency:
    """Вехи не срабатывают, потому что срабатывать им не от чего.

    Класс сторожил идемпотентность порогов «50% / 100% / 150%»: чтобы
    «Половина дня — отличный темп!» не мигало дважды за день. Порог —
    ПРОИЗВОДНАЯ ориентира (``norm × threshold / 100``), а ориентира по
    жидкости больше нет ни у кого: формула 30 мл × вес снята до
    утверждения методики (§82; §85 раздел 4).

    Поздравить человека с выполнением числа, которое мы ему придумали,
    хуже, чем промолчать, — и «норма выполнена 💧» было самым громким
    местом, где выдумка возвращалась ему как достижение.

    Утверждения перевёрнуты: ни один порог не срабатывает ни при каком
    объёме, включая тот, что раньше давал все три подряд. Записи при
    этом пишутся и суммируются — снимается ориентир, не факт.
    """

    def test_no_threshold_fires_at_any_volume(self, proxy_user, seed, headers):
        c = APIClient()
        # Те же объёмы, что раньше давали 50%, 100% и 150% от 2000 мл.
        r1 = _post_water(c, {"ml": 1100}, headers)
        r2 = _post_water(c, {"ml": 950}, headers)   # было «норма выполнена»
        r3 = _post_water(c, {"ml": 1000}, headers)  # было «с запасом»
        for r in (r1, r2, r3):
            assert not (r.json()["data"].get("milestone_text") or "")
        # POSITIVE: выпитое посчитано — 1100 + 950 + 1000.
        assert r3.json()["data"]["today_total_water_ml"] == 3050

    def test_undo_still_works_without_milestones(self, proxy_user, seed, headers):
        """Отмена записи цела: снят порог, а не путь назад."""
        c = APIClient()
        _post_water(c, {"ml": 500}, headers)
        r2 = _post_water(c, {"ml": 500}, headers)
        entry_id = r2.json()["data"]["entry_id"]
        assert not (r2.json()["data"].get("milestone_text") or "")
        del_resp = c.delete(
            f"{WATER_URL}{entry_id}/", **headers,
        )
        assert del_resp.status_code == status.HTTP_200_OK
        # Re-add water — must NOT re-fire 50%
        r3 = _post_water(c, {"ml": 500}, headers)
        assert r3.json()["data"]["milestone_text"] is None


# ---------------------------------------------------------------------------
# Alcohol hint
# ---------------------------------------------------------------------------


class TestAlcoholHint:
    def test_alcohol_sets_hint_flag(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 150, "beverage_slug": "vino_krasnoe"}, headers)
        body = resp.json()["data"]
        assert body["alcohol_recovery_hint"] is True

    def test_non_alcohol_no_hint(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 250, "beverage_slug": "voda"}, headers)
        body = resp.json()["data"]
        assert body["alcohol_recovery_hint"] is False


# ---------------------------------------------------------------------------
# Caffeine warning (pregnant)
# ---------------------------------------------------------------------------


class TestCaffeineWarning:
    @patch(
        "nutrition.services.water_entry_service._load_nutrition_context",
        return_value=NutritionContext(pregnant=True),
    )
    def test_pregnant_over_threshold_warns(self, _ctx, proxy_user, seed, headers):
        c = APIClient()
        # 600 ml espresso × 180 mg/100ml = 1080 mg — well over 200 mg
        resp = _post_water(c, {"ml": 200, "beverage_slug": "espresso"}, headers)
        assert resp.json()["data"]["caffeine_warning"] is not None

    @patch(
        "nutrition.services.water_entry_service._load_nutrition_context",
        return_value=NutritionContext(pregnant=True),
    )
    def test_pregnant_under_threshold_no_warning(
        self, _ctx, proxy_user, seed, headers,
    ):
        c = APIClient()
        # 100 ml black coffee × 40 mg/100ml = 40 mg — below threshold
        resp = _post_water(c, {"ml": 100, "beverage_slug": "kofe_chernyi"}, headers)
        assert resp.json()["data"]["caffeine_warning"] is None

    def test_non_pregnant_default_no_warning(self, proxy_user, seed, headers):
        c = APIClient()
        resp = _post_water(c, {"ml": 200, "beverage_slug": "espresso"}, headers)
        assert resp.json()["data"]["caffeine_warning"] is None


# ---------------------------------------------------------------------------
# Eating disorder mode
# ---------------------------------------------------------------------------


class TestEatingDisorderMode:
    @patch(
        "nutrition.services.water_entry_service._load_nutrition_context",
        return_value=NutritionContext(eating_disorder=True),
    )
    def test_strips_kcal_milestone_alcohol_hint(
        self, _ctx, proxy_user, seed, headers,
    ):
        c = APIClient()
        # Big drink that would normally fire 50% milestone + alcohol hint
        resp = _post_water(c, {"ml": 1500, "beverage_slug": "vino_krasnoe"}, headers)
        body = resp.json()["data"]
        assert body["kcal"] == 0
        assert body["protein_g"] == 0
        assert body["milestone_text"] is None
        assert body["alcohol_recovery_hint"] is False
        assert body["caffeine_warning"] is None

    @patch(
        "nutrition.services.water_entry_service._load_nutrition_context",
        return_value=NutritionContext(eating_disorder=True),
    )
    def test_persistence_still_records_macros_internally(
        self, _ctx, proxy_user, seed, headers,
    ):
        """The wire response is stripped; the DB still keeps the truth so a
        future profile change re-exposes the data."""
        c = APIClient()
        resp = _post_water(c, {"ml": 200, "beverage_slug": "latte"}, headers)
        entry = WaterEntry.objects.get(id=resp.json()["data"]["entry_id"])
        assert entry.kcal > 0


# ---------------------------------------------------------------------------
# Soft delete + restore
# ---------------------------------------------------------------------------


class TestSoftDeleteAndRestore:
    def test_delete_soft_removes_from_total(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 500}, headers)
        entry_id = r1.json()["data"]["entry_id"]
        del_resp = c.delete(f"{WATER_URL}{entry_id}/", **headers)
        assert del_resp.status_code == status.HTTP_200_OK
        body = del_resp.json()["data"]
        assert body["deleted"] is True
        assert body["today_total_water_ml"] == 0
        # Soft-delete: row still present, deleted_at set
        entry = WaterEntry.objects.get(id=entry_id)
        assert entry.deleted_at is not None

    def test_delete_cascades_food_log(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 200, "beverage_slug": "latte"}, headers)
        entry = WaterEntry.objects.get(id=r1.json()["data"]["entry_id"])
        food_log_id = entry.food_log_id
        assert food_log_id is not None
        c.delete(f"{WATER_URL}{entry.id}/", **headers)
        assert not FoodLog.objects.filter(id=food_log_id).exists()
        entry.refresh_from_db()
        assert entry.food_log_id is None

    def test_restore_within_window(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 250}, headers)
        entry_id = r1.json()["data"]["entry_id"]
        c.delete(f"{WATER_URL}{entry_id}/", **headers)
        restore = c.post(f"{WATER_URL}{entry_id}/restore/", **headers)
        assert restore.status_code == status.HTTP_200_OK
        body = restore.json()["data"]
        assert body["restored"] is True
        assert body["today_total_water_ml"] == 250
        entry = WaterEntry.objects.get(id=entry_id)
        assert entry.deleted_at is None

    def test_restore_after_window_returns_410(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 250}, headers)
        entry_id = r1.json()["data"]["entry_id"]
        c.delete(f"{WATER_URL}{entry_id}/", **headers)
        # Move deleted_at into the past beyond the 15-min window
        WaterEntry.objects.filter(id=entry_id).update(
            deleted_at=datetime.now(dt_tz.utc) - timedelta(minutes=20),
        )
        restore = c.post(f"{WATER_URL}{entry_id}/restore/", **headers)
        assert restore.status_code == status.HTTP_410_GONE

    def test_delete_other_users_entry_returns_404(self, proxy_user, seed, headers):
        c = APIClient()
        # Create entry as user A
        r1 = _post_water(c, {"ml": 250}, headers)
        entry_id = r1.json()["data"]["entry_id"]
        # Try to delete as user B (different external id)
        User.objects.create(username="bot:999", role="client", is_proxy=True)
        other_headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:999",
        }
        resp = c.delete(f"{WATER_URL}{entry_id}/", **other_headers)
        assert resp.status_code == status.HTTP_404_NOT_FOUND


# ---------------------------------------------------------------------------
# GET /today/
# ---------------------------------------------------------------------------


class TestTodayEndpoint:
    def test_empty_returns_zeros(self, proxy_user, seed, headers):
        c = APIClient()
        resp = c.get(TODAY_URL, **headers)
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["entries"] == []
        assert body["today_total_water_ml"] == 0
        assert "today_norm_water_ml" not in body

    def test_aggregates_per_category_cups(self, proxy_user, seed, headers):
        c = APIClient()
        _post_water(c, {"ml": 200, "beverage_slug": "kofe_chernyi"}, headers)
        _post_water(c, {"ml": 200, "beverage_slug": "latte"}, headers)
        _post_water(c, {"ml": 200, "beverage_slug": "chai_zelenyi"}, headers)
        _post_water(c, {"ml": 250}, headers)  # plain water, neither
        resp = c.get(TODAY_URL, **headers)
        body = resp.json()["data"]
        assert body["today_total_coffee_cups"] == 2
        assert body["today_total_tea_cups"] == 1
        assert body["today_caffeine_mg"] > 0

    def test_excludes_soft_deleted(self, proxy_user, seed, headers):
        c = APIClient()
        r1 = _post_water(c, {"ml": 500}, headers)
        _post_water(c, {"ml": 250}, headers)
        c.delete(f"{WATER_URL}{r1.json()['data']['entry_id']}/", **headers)
        resp = c.get(TODAY_URL, **headers)
        body = resp.json()["data"]
        assert len(body["entries"]) == 1
        assert body["today_total_water_ml"] == 250


# ---------------------------------------------------------------------------
# Purge task
# ---------------------------------------------------------------------------


class TestPurgeOlderThan90Days:
    def test_purges_old_soft_deleted(self, proxy_user, seed):
        # Insert an old soft-deleted row directly
        old = WaterEntry.objects.create(
            user=proxy_user, ts=datetime.now(dt_tz.utc) - timedelta(days=120),
            ml=250, water_ml=250.0,
            deleted_at=datetime.now(dt_tz.utc) - timedelta(days=100),
            deleted_reason=WaterEntry.DeletedReason.USER_UNDO,
        )
        # And a recent soft-deleted row
        recent = WaterEntry.objects.create(
            user=proxy_user, ts=datetime.now(dt_tz.utc),
            ml=250, water_ml=250.0,
            deleted_at=datetime.now(dt_tz.utc) - timedelta(days=5),
            deleted_reason=WaterEntry.DeletedReason.USER_UNDO,
        )
        purged = purge_deleted_water_entries(older_than_days=90)
        assert purged == 1
        assert not WaterEntry.objects.filter(id=old.id).exists()
        assert WaterEntry.objects.filter(id=recent.id).exists()


class TestNoAnketaNoNorm:
    """Ориентира нет ни с анкетой, ни без неё.

    Класс заводился как пара к ``proxy_user``, где анкета есть. Разницы
    больше нет: формула ``30 мл × вес`` снята для всех до утверждения
    методики (§82; §85 раздел 4). Класс оставлен — он проверяет самый
    невыгодный для правки случай, человека БЕЗ профиля вовсе, и держит
    вторую половину утверждения: запись при этом пишется.
    """

    @pytest.fixture
    def bare_user(self, db):
        return User.objects.create(
            username="bot:303", role="client", is_proxy=True,
        )

    def test_norm_is_zero_and_no_milestone_fires(self, bare_user, seed):
        c = APIClient()
        headers = {
            "HTTP_X_SERVICE_TOKEN": SERVICE_TOKEN,
            "HTTP_X_EXTERNAL_USER_ID": "bot:303",
        }

        resp = _post_water(c, {"ml": 1000}, headers)

        assert resp.status_code == status.HTTP_201_CREATED, resp.json()
        body = resp.json()["data"]
        # POSITIVE: выпитое записано и посчитано — правда не теряется.
        assert body["today_total_water_ml"] == 1000
        # NEGATIVE: ни 2000, ни поздравления с половиной несуществующей
        # нормы. И больше даже не ноль: ноль был внутренним словом
        # «нормы нет», а наружу уезжал значением — ключа теперь нет.
        assert "today_norm_water_ml" not in body
        assert not (body.get("milestone_text") or "")
