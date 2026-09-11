"""Tests for water tracker endpoints + WaterService.

Per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION:

POST /nutrition/water
    Request:  { amount_ml: 150 | 200 | 250 | 350 | 500 }
    Response: { water_ml, log_id } — ориентира и процента нет (§82)

DELETE /nutrition/water/{id}
    Response: WaterLogResponse (with updated aggregate)

GET /nutrition/water/today
    Response: { logs: [{id, amount_ml, logged_at}], water_ml }
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_tz

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from nutrition.models import WaterLog


pytestmark = pytest.mark.django_db


CREATE_URL = "/api/v1/nutrition/water/"
TODAY_URL = "/api/v1/nutrition/water/today/"


@pytest.fixture
def client_user(db):
    from nutrition.models import NutritionProfile
    from users.models import Profile, User

    u = User.objects.create_user(
        username="wat-client", password="x", role="client",
        phone="+79995550000",
    )
    Profile.objects.filter(user=u).update(full_name="Wat", city="Penza")
    # Анкета питания — часть предусловия каждого теста про НОРМУ.
    #
    # Раньше норма приезжала из ``settings.NUTRITION_DEFAULT_WATER_GOAL_ML``
    # (2000 мл = ровно восемь стаканов по 250) и потому была у всех, в том
    # числе у людей без анкеты: чужое число показывалось человеку как его
    # дневная цель. Числа тестов ниже сохранены, изменился ИСТОЧНИК —
    # норма принадлежит человеку. Обратную половину держит
    # ``TestNoAnketaNoGoal`` в конце файла.
    NutritionProfile.objects.create(user=u, daily_water_ml=2000)
    return u


@pytest.fixture
def other_client_user(db):
    from users.models import User

    u = User.objects.create_user(
        username="wat-other", password="x", role="client",
        phone="+79995550001",
    )
    return u


@pytest.fixture
def auth_client(client_user):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c


def _make_water(*, user, amount, when=None) -> WaterLog:
    return WaterLog.objects.create(
        user=user,
        amount_ml=amount,
        logged_at=when or datetime.now(dt_tz.utc),
    )


# ---------------------------------------------------------------------------
# Auth + app-type — apply to all three endpoints
# ---------------------------------------------------------------------------


class TestAuthAndAppType:
    def test_post_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.post(CREATE_URL, {"amount_ml": 250}, format="json")
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED

    def test_post_pro_app_type_returns_403(self, client_user):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "pro"
        c.force_authenticate(user=client_user)
        resp = c.post(CREATE_URL, {"amount_ml": 250}, format="json")
        assert resp.status_code == status.HTTP_403_FORBIDDEN

    def test_today_unauthenticated_returns_401(self):
        c = APIClient()
        c.defaults["HTTP_X_APP_TYPE"] = "client"
        resp = c.get(TODAY_URL)
        assert resp.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# POST validation
# ---------------------------------------------------------------------------


class TestCreateValidation:
    @pytest.mark.parametrize("bad_amount", [0, 100, 199, 1000, -50])
    def test_invalid_amount_rejected(self, auth_client, bad_amount):
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": bad_amount}, format="json",
        )
        assert resp.status_code == status.HTTP_400_BAD_REQUEST
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_missing_amount_rejected(self, auth_client):
        resp = auth_client.post(CREATE_URL, {}, format="json")
        assert resp.status_code == status.HTTP_400_BAD_REQUEST

    @pytest.mark.parametrize("amount", [150, 200, 250, 350, 500])
    def test_all_spec_amounts_accepted(self, auth_client, amount):
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": amount}, format="json",
        )
        assert resp.status_code == status.HTTP_200_OK


# ---------------------------------------------------------------------------
# POST happy path
# ---------------------------------------------------------------------------


class TestCreateHappyPath:
    def test_creates_log_and_returns_aggregate(self, auth_client, client_user):
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": 250}, format="json",
        )
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        # Ключей ориентира в ответе НЕТ — ни `water_goal_ml`, ни
        # `water_pct`. Формула 30 мл × вес снята до утверждения методики
        # (§82, §85 раздел 4), и отсутствие доезжает отсутствием ключа,
        # а не нулём: ключ со значением 0 потребитель вправе показать
        # как «0 мл цели · 0 %».
        #
        # Процент ушёл вместе с ориентиром, потому что он его ПРОИЗВОДНАЯ:
        # доли от несуществующей нормы не бывает.
        assert set(body.keys()) == {"water_ml", "log_id"}
        # POSITIVE: выпитое на месте — снимается ориентир, не факт.
        assert body["water_ml"] == 250

        log = WaterLog.objects.get(id=body["log_id"])
        assert log.user_id == client_user.id
        assert log.amount_ml == 250

    def test_aggregate_sums_existing_today_logs(
        self, auth_client, client_user,
    ):
        _make_water(user=client_user, amount=250)
        _make_water(user=client_user, amount=350)
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": 200}, format="json",
        )
        body = resp.json()["data"]
        assert body["water_ml"] == 800

    def test_pct_caps_at_100(self, auth_client, client_user):
        # Already over goal — pct should clamp to 100.
        _make_water(user=client_user, amount=500)
        _make_water(user=client_user, amount=500)
        _make_water(user=client_user, amount=500)
        _make_water(user=client_user, amount=500)
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": 250}, format="json",
        )
        body = resp.json()["data"]
        # Выпитое считается по-прежнему...
        assert body["water_ml"] == 2250
        # ...а «потолка в 100%» больше нет, потому что нет и процента.
        # Тест назывался ``test_pct_caps_at_100`` и сторожил обрезку
        # шкалы; сторожить стало нечего — шкалы нет, пока нет ориентира.
        assert "water_pct" not in body

    def test_other_users_water_not_counted(
        self, auth_client, client_user, other_client_user,
    ):
        _make_water(user=other_client_user, amount=500)
        resp = auth_client.post(
            CREATE_URL, {"amount_ml": 250}, format="json",
        )
        assert resp.json()["data"]["water_ml"] == 250


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------


class TestDelete:
    def test_deletes_and_returns_updated_aggregate(
        self, auth_client, client_user,
    ):
        _make_water(user=client_user, amount=250)
        log = _make_water(user=client_user, amount=350)
        resp = auth_client.delete(f"/api/v1/nutrition/water/{log.id}/")
        assert resp.status_code == status.HTTP_200_OK, resp.json()
        body = resp.json()["data"]
        assert body["water_ml"] == 250
        assert body["log_id"] == str(log.id)
        assert WaterLog.objects.filter(id=log.id).count() == 0

    def test_other_users_log_returns_404(
        self, auth_client, other_client_user,
    ):
        log = _make_water(user=other_client_user, amount=250)
        resp = auth_client.delete(f"/api/v1/nutrition/water/{log.id}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND
        assert resp.json()["error"]["code"] == "NOT_FOUND"
        # Not actually deleted.
        assert WaterLog.objects.filter(id=log.id).count() == 1

    def test_unknown_id_returns_404(self, auth_client):
        from uuid import uuid4
        resp = auth_client.delete(f"/api/v1/nutrition/water/{uuid4()}/")
        assert resp.status_code == status.HTTP_404_NOT_FOUND

    def test_delete_yesterday_log_returns_yesterdays_aggregate(
        self, auth_client, client_user,
    ):
        # User undoes a glass from yesterday — response aggregate should
        # reflect yesterday's totals (post-delete), not today's, so the
        # mobile UI on yesterday's diary view updates correctly.
        from datetime import timedelta
        yest = datetime.now(dt_tz.utc) - timedelta(days=1)
        yest = yest.replace(hour=12, minute=0, second=0, microsecond=0)
        kept = _make_water(user=client_user, amount=250, when=yest)
        deleted = _make_water(user=client_user, amount=350, when=yest)
        # Today's log so we can verify the response is NOT today's totals.
        _make_water(user=client_user, amount=500)
        resp = auth_client.delete(f"/api/v1/nutrition/water/{deleted.id}/")
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        # Yesterday: 250 (kept) remains after deleting 350.
        assert body["water_ml"] == 250
        assert WaterLog.objects.filter(id=kept.id).count() == 1


# ---------------------------------------------------------------------------
# GET /water/today
# ---------------------------------------------------------------------------


class TestToday:
    def test_empty_returns_empty_logs_zero_total(self, auth_client):
        resp = auth_client.get(TODAY_URL)
        assert resp.status_code == status.HTTP_200_OK
        body = resp.json()["data"]
        assert set(body.keys()) == {"logs", "water_ml"}
        assert body["logs"] == []
        assert body["water_ml"] == 0
        # Ориентира нет — ключа нет. Ноль здесь был бы «норма ноль мл».
        assert "water_goal_ml" not in body

    def test_lists_today_logs_in_order(self, auth_client, client_user):
        # Anchor inside today's UTC day so a CI run near midnight doesn't
        # straddle the day boundary and drop the earliest entry into
        # yesterday — surfaced 2026-05-05 when CI ran at 01:14 UTC.
        today_noon = datetime.now(dt_tz.utc).replace(
            hour=12, minute=0, second=0, microsecond=0,
        )
        _make_water(user=client_user, amount=250, when=today_noon)
        _make_water(user=client_user, amount=350, when=today_noon + timedelta(hours=1))
        _make_water(user=client_user, amount=200, when=today_noon + timedelta(hours=2))
        resp = auth_client.get(TODAY_URL)
        body = resp.json()["data"]
        assert body["water_ml"] == 800
        assert [log["amount_ml"] for log in body["logs"]] == [250, 350, 200]

    def test_excludes_yesterday(self, auth_client, client_user):
        yesterday = datetime.now(dt_tz.utc) - timedelta(days=1)
        # Force into yesterday's UTC day window.
        _make_water(
            user=client_user, amount=500,
            when=yesterday.replace(hour=12, minute=0, second=0),
        )
        _make_water(user=client_user, amount=250)
        resp = auth_client.get(TODAY_URL)
        body = resp.json()["data"]
        assert body["water_ml"] == 250
        assert len(body["logs"]) == 1

    def test_excludes_other_users(
        self, auth_client, client_user, other_client_user,
    ):
        _make_water(user=other_client_user, amount=500)
        _make_water(user=client_user, amount=250)
        body = auth_client.get(TODAY_URL).json()["data"]
        assert body["water_ml"] == 250
        assert len(body["logs"]) == 1


# ---------------------------------------------------------------------------
# Integration with /nutrition/summary — Slice 3c stub now lives
# ---------------------------------------------------------------------------


class TestSummaryIntegration:
    def test_summary_water_now_reflects_water_logs(
        self, auth_client, client_user,
    ):
        # Make sure today's water is summed by the summary endpoint.
        _make_water(user=client_user, amount=250)
        _make_water(user=client_user, amount=350)
        today_iso = datetime.now(dt_tz.utc).date().isoformat()
        resp = auth_client.get(
            "/api/v1/nutrition/summary/", {"date": today_iso},
        )
        body = resp.json()["data"]
        assert body["water_ml"] == 600
        assert "water_goal_ml" not in body


class TestNoAnketaNoGoal:
    """Ориентира нет ни с анкетой, ни без неё.

    Класс заводился как пара к фикстуре ``client_user``: там анкета есть
    и норма живёт, здесь анкеты нет и норма ноль. Разницы больше нет —
    формула ``30 мл × вес`` снята для всех до утверждения методики
    (§82; §85 раздел 4 — справочные 2200/3000 мл по полу придут
    отдельным срезом).

    Изменилось и КАК выражается отсутствие: было нулём, стало ``None``.
    Ноль внутри модуля читался правильно, но наружу уезжал значением —
    ключ в JSON был, и потребитель вправе показать «0 мл · 0 %».
    """

    def test_water_goal_is_absent_without_a_profile(self, other_client_user):
        from nutrition.services.water_service import WaterService

        WaterLog.objects.create(
            user=other_client_user,
            amount_ml=250,
            logged_at=datetime.now(dt_tz.utc),
        )

        agg = WaterService().aggregate_for_day(
            other_client_user.id, datetime.now(dt_tz.utc).date()
        )

        # POSITIVE: выпитое посчитано — правду вместе с выдумкой не теряем.
        assert agg.water_ml == 250
        # NEGATIVE: ни 2000, ни любого другого придуманного числа, и
        # больше даже не ноль. Ноль был внутренним словом «нормы нет» и
        # наружу уезжал значением; теперь и внутри стоит `None`.
        assert agg.water_goal_ml is None
        assert agg.water_pct is None
