"""Дневник F10 — неделя по дням: ручка списка за период (DRF-2099, §48 п.7).

``GET /api/v1/nutrition/internal/diary/days/?from=&to=`` → по каждому дню
периода строка ``{date, meals_count, kcal|null, has_entries}``; ≤ 28 дней;
без параметров — 7 дней до сегодня. **Без** retention-механики: пустая
неделя — семь строк с ``has_entries: false``, и ни одного сообщения.

**Сутки — по поясу человека.** Пояс — ``NutritionProfile.timezone``, если
задан и валиден, иначе UTC — тот же предикат, что у воды v3
(``water_entry_service``); умолчание UTC, а не MSK: так уже считает вода
и так стоит профиль по умолчанию, тихая подмена на MSK сдвинула бы дни
всем. Тем же предикатом — окно суток сводки ``?date=`` (иначе «открыть
день» с недельного экрана показывал бы не те записи: 01:30 MSK — это
22:30 UTC прошлого дня) и ``get_commitment_days`` («тот же ответ» по
листу). Поведение меняется ТОЛЬКО у профилей с заданным поясом.

Красное до правки объявлено поимённо до прогона: 9 красных / 2 контроля.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_tz

import pytest
from rest_framework.test import APIClient

from nutrition.models import FoodLog, NutritionProfile
from users.models import User

pytestmark = pytest.mark.django_db

SERVICE_TOKEN = "test-token-DRF-2099"
OWNER = "bot:2099"
STRANGER = "bot:2099-stranger"
DAYS_URL = "/api/v1/nutrition/internal/diary/days/"
SUMMARY_URL = "/api/v1/nutrition/internal/summary/"

#: Неделя фиксирована: пн 2026-09-14 … вс 2026-09-20.
MONDAY = date(2026, 9, 14)
SUNDAY = date(2026, 9, 20)


@pytest.fixture(autouse=True)
def _set_service_token(settings):
    settings.NUTRITION_SERVICE_TOKEN = SERVICE_TOKEN


@pytest.fixture
def owner(db) -> User:
    return User.objects.create(username=OWNER, role="client", is_proxy=True)


@pytest.fixture
def stranger(db) -> User:
    return User.objects.create(username=STRANGER, role="client", is_proxy=True)


@pytest.fixture
def moscow(owner) -> NutritionProfile:
    return NutritionProfile.objects.create(user=owner, timezone="Europe/Moscow")


def _client(external_id: str = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_X_SERVICE_TOKEN"] = SERVICE_TOKEN
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_id
    return c


def _log(user, at: datetime, *, dish: str = "борщ", kcal: float = 100.0) -> FoodLog:
    return FoodLog.objects.create(
        user=user, dish_name=dish, portion_multiplier=1.0, calories=kcal,
        protein_g=1.0, fat_g=1.0, carbs_g=1.0, meal_type="lunch",
        entry_origin=FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED, logged_at=at,
    )


def _days(client: APIClient, **params) -> dict:
    resp = client.get(DAYS_URL, params)
    assert resp.status_code == 200, resp.content[:400]
    return resp.json()["data"]


def _week(client: APIClient = None) -> list[dict]:
    return _days(client or _client(), **{"from": MONDAY.isoformat(), "to": SUNDAY.isoformat()})["days"]


class TestTheWeekIsSevenRows:
    def test_empty_week_is_seven_rows_with_has_entries_false_and_kcal_null(self, owner) -> None:
        rows = _week()

        assert [r["date"] for r in rows] == [(MONDAY + timedelta(days=i)).isoformat() for i in range(7)]
        assert all(r["has_entries"] is False and r["meals_count"] == 0 and r["kcal"] is None for r in rows)
        # Только факт — ни одного слова о пропусках (§48 п.7).
        text = str(rows).lower()
        assert "пропу" not in text and "streak" not in text and "missed" not in text

    def test_a_day_with_entries_carries_meals_count_and_kcal(self, owner) -> None:
        wed = datetime(2026, 9, 16, 12, 0, tzinfo=dt_tz.utc)
        _log(owner, wed, kcal=300.0)
        _log(owner, wed + timedelta(hours=6), dish="омлет", kcal=200.0)

        rows = {r["date"]: r for r in _week()}

        # DRF-2371 — в строке дня появилось число записей без расчёта:
        # «0 ккал» о дне, в котором ели, было бы утверждением о расчёте.
        assert rows["2026-09-16"] == {
            "date": "2026-09-16", "meals_count": 2, "kcal": 500.0,
            "has_entries": True, "uncounted_meals": 0,
        }
        assert sum(1 for r in rows.values() if r["has_entries"]) == 1

    def test_route_returns_seven_rows_for_the_default_window(self, owner) -> None:
        """Без параметров — семь дней до сегодня по поясу человека."""
        data = _days(_client())
        assert len(data["days"]) == 7
        assert data["days"][-1]["date"] == datetime.now(dt_tz.utc).date().isoformat()  # UTC — пояса нет
        assert data["timezone"] == "UTC"


class TestDaysAreLocalDays:
    def test_a_late_evening_entry_belongs_to_the_local_day(self, owner, moscow) -> None:
        """22:30 UTC вторника = 01:30 MSK среды: день — среда."""
        _log(owner, datetime(2026, 9, 15, 22, 30, tzinfo=dt_tz.utc))

        data = _days(_client(), **{"from": MONDAY.isoformat(), "to": SUNDAY.isoformat()})
        rows = {r["date"]: r for r in data["days"]}

        assert data["timezone"] == "Europe/Moscow"
        assert rows["2026-09-16"]["has_entries"] is True
        assert rows["2026-09-15"]["has_entries"] is False

    def test_the_same_day_opened_via_date_shows_that_entry(self, owner, moscow) -> None:
        """«Открыть день» с недели — сводка ?date= того же ЛОКАЛЬНОГО дня."""
        _log(owner, datetime(2026, 9, 15, 22, 30, tzinfo=dt_tz.utc), dish="ночной борщ")

        resp = _client().get(SUMMARY_URL, {"date": "2026-09-16"})

        assert resp.status_code == 200, resp.content[:400]
        assert [e["dish_name"] for e in resp.json()["data"]["entries"]] == ["ночной борщ"]
        # И в UTC-дне (15-е) её больше нет — день один, не два.
        prev = _client().get(SUMMARY_URL, {"date": "2026-09-15"}).json()["data"]["entries"]
        assert prev == []

    def test_commitment_days_count_by_the_local_day(self, owner, moscow) -> None:
        from nutrition.services.commitment_service import get_commitment_days

        now = datetime.now(dt_tz.utc)
        # Две записи вокруг полуночи MSK одного календарного UTC-дня —
        # по MSK это ДВА дня (21:30 UTC = 00:30 MSK следующего дня).
        base = (now - timedelta(days=3)).replace(hour=20, minute=0, second=0, microsecond=0)
        _log(owner, base)
        _log(owner, base + timedelta(hours=1, minutes=30))

        assert get_commitment_days(owner) == 2

    def test_no_timezone_means_utc_days(self, owner) -> None:
        """Контроль: профиль без пояса (умолчание) — сутки UTC, как сегодня."""
        NutritionProfile.objects.create(user=owner)  # timezone default "UTC"
        _log(owner, datetime(2026, 9, 15, 22, 30, tzinfo=dt_tz.utc))

        data = _days(_client(), **{"from": MONDAY.isoformat(), "to": SUNDAY.isoformat()})
        rows = {r["date"]: r for r in data["days"]}

        assert data["timezone"] == "UTC"
        assert rows["2026-09-15"]["has_entries"] is True

    def test_summary_without_a_timezone_is_unchanged(self, owner) -> None:
        """Контроль: без пояса окно сводки — UTC, как было."""
        _log(owner, datetime(2026, 9, 15, 22, 30, tzinfo=dt_tz.utc), dish="utc-ужин")
        resp = _client().get(SUMMARY_URL, {"date": "2026-09-15"})
        assert [e["dish_name"] for e in resp.json()["data"]["entries"]] == ["utc-ужин"]


class TestBoundsAndIsolation:
    def test_span_over_28_days_is_400(self, owner) -> None:
        resp = _client().get(DAYS_URL, {"from": "2026-08-01", "to": "2026-09-01"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_from_after_to_is_400(self, owner) -> None:
        resp = _client().get(DAYS_URL, {"from": "2026-09-20", "to": "2026-09-14"})
        assert resp.status_code == 400

    def test_a_stranger_sees_only_their_own_days(self, owner, stranger) -> None:
        _log(owner, datetime(2026, 9, 16, 12, 0, tzinfo=dt_tz.utc))

        theirs = _week(_client(STRANGER))
        mine = _week(_client(OWNER))

        assert sum(r["meals_count"] for r in mine) == 1  # присутствие
        assert all(not r["has_entries"] for r in theirs)


class TestNoGapsNoDuplicatesAcrossDst:
    @pytest.mark.parametrize(
        ("tz", "day_from", "day_to"),
        [
            # Europe/Berlin: переход на летнее время 2026-03-29, на зимнее 2026-10-25.
            ("Europe/Berlin", date(2026, 3, 26), date(2026, 4, 1)),
            ("Europe/Berlin", date(2026, 10, 22), date(2026, 10, 28)),
        ],
        ids=["spring-forward", "fall-back"],
    )
    def test_days_are_exactly_span_plus_one_without_gaps_or_duplicates(
        self, owner, tz: str, day_from: date, day_to: date,
    ) -> None:
        """TruncDate по поясу на границе DST легко даёт дубль/дыру — стережём
        сам список дат: ровно span+1, подряд, без повторов; записи на самой
        границе (01:30 и 03:30 локального) остаются в своём дне."""
        from zoneinfo import ZoneInfo

        NutritionProfile.objects.create(user=owner, timezone=tz)
        zone = ZoneInfo(tz)
        switch = day_from + timedelta(days=3)
        for local_hour in (1, 3):
            _log(owner, datetime(switch.year, switch.month, switch.day, local_hour, 30, tzinfo=zone))

        data = _days(_client(), **{"from": day_from.isoformat(), "to": day_to.isoformat()})
        dates = [r["date"] for r in data["days"]]

        expected = [(day_from + timedelta(days=i)).isoformat() for i in range((day_to - day_from).days + 1)]
        assert dates == expected  # присутствие: span+1, подряд; отсюда же — без дублей и дыр
        assert len(set(dates)) == len(dates)
        rows = {r["date"]: r for r in data["days"]}
        assert rows[switch.isoformat()]["meals_count"] == 2
        assert sum(r["meals_count"] for r in rows.values()) == 2
