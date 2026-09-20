"""Срок цели (DRF-2173, H01-2) — бесконтактная половина каталога.

Решение владельца 20.09 («цена и срок нужны»). Здесь:

* ``ClientGoal.target_date`` — DateField(null): срок необязателен, по умолчанию
  его нет; null → в документе ``known.goal.target_date`` = null, строки на
  экране нет (§103).
* ``goals/deadline.py`` — детерминированный разбор того, что человек написал
  или выбрал: «через месяц», «к лету», «до 1 ноября», «01.11.2026», «через 2
  недели»; ни LLM, ни угадывания. Таблицей: одна строка — один вход.
* Границы: не раньше сегодня, не дальше двух лет — иначе ``DeadlineError``
  словами (API отдаёт 400 словами; это проверяет вторая половина — шаг
  анкеты, после слияния К-2).
* ``target_date_passed`` в документе — факт «срок прошёл» считает сервер, экран
  дату с сегодняшним днём не сравнивает.

Чего здесь нет: напоминаний, процентов «времени прошло», пересчёта плана
(п.4 листа: §49/§82 — план про действия).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from django.db import connection

from goals import deadline
from goals.decision_context import build_decision_context
from goals.models import ClientGoal
from users.models import User

TODAY = date(2026, 9, 20)


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="bot:goals-2173", password="x", role="client",
        phone="+79995002173", is_proxy=True,
    )


# ---------------------------------------------------------------------------
# Модель + документ
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTargetDateOnGoal:
    def test_field_is_nullable_and_absent_by_default(self, customer):
        goal = ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="miniapp",
        )
        goal.refresh_from_db()
        assert goal.target_date is None

    def test_document_carries_null_when_no_deadline(self, customer):
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="miniapp",
        )
        goal = build_decision_context(customer)["known"]["goal"]
        assert "target_date" in goal
        assert goal["target_date"] is None
        assert goal["target_date_passed"] is False

    def test_document_carries_the_iso_date(self, customer):
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="miniapp",
            target_date=date.today() + timedelta(days=40),
        )
        goal = build_decision_context(customer)["known"]["goal"]
        assert goal["target_date"] == (date.today() + timedelta(days=40)).isoformat()
        assert goal["target_date_passed"] is False

    def test_document_says_when_the_deadline_has_passed(self, customer):
        """Срок в прошлом — факт документа; экран рисует «Срок прошёл — обновить?» по нему."""
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="miniapp",
            target_date=date.today() - timedelta(days=1),
        )
        goal = build_decision_context(customer)["known"]["goal"]
        assert goal["target_date_passed"] is True

    def test_goal_text_is_not_touched_by_the_deadline(self, customer):
        """§48 «оставь как в чате»: срок живёт в своём поле, текст цели дословен."""
        goal = ClientGoal.objects.create(
            client=customer, goal_text="хочу похудеть к отпуску", source_channel="bot",
        )
        deadline.apply_target_date(goal, date.today() + timedelta(days=30))
        goal.refresh_from_db()
        assert goal.goal_text == "хочу похудеть к отпуску"
        assert goal.target_date == date.today() + timedelta(days=30)

    def test_column_exists_in_the_database(self, db):
        columns = {c.name for c in connection.introspection.get_table_description(
            connection.cursor(), ClientGoal._meta.db_table,
        )}
        assert "target_date" in columns


# ---------------------------------------------------------------------------
# Разбор — таблицей
# ---------------------------------------------------------------------------


class TestResolveTargetDate:
    @pytest.mark.parametrize(
        ("option_key", "text", "expected"),
        [
            # чипы
            (deadline.NO_DEADLINE, None, None),
            (deadline.IN_MONTH, None, date(2026, 10, 20)),
            # «к лету» = 1 июня ближайшего будущего лета (умолчание, на владельца)
            (deadline.BY_SUMMER, None, date(2027, 6, 1)),
            # свободный текст: день + месяц по-русски, год — ближайший будущий
            (None, "до 1 ноября", date(2026, 11, 1)),
            (None, "к 15 декабря", date(2026, 12, 15)),
            (None, "1 марта", date(2027, 3, 1)),  # март уже прошёл → следующий год
            (None, "до 20 сентября", date(2027, 9, 20)),  # «сегодня» — не раньше сегодня → через год
            (None, "До 1 Ноября 2026", date(2026, 11, 1)),
            (None, "01.11.2026", date(2026, 11, 1)),
            (None, "1.11", date(2026, 11, 1)),
            (None, "2026-11-01", date(2026, 11, 1)),
            # относительные
            (None, "через месяц", date(2026, 10, 20)),
            (None, "через 2 месяца", date(2026, 11, 20)),
            (None, "через 3 недели", date(2026, 10, 11)),
            (None, "через полгода", date(2027, 3, 20)),
            (None, "через год", date(2027, 9, 20)),
            # сезоны — первое число сезона, ближайшее будущее
            (None, "к лету", date(2027, 6, 1)),
            (None, "к зиме", date(2026, 12, 1)),
            (None, "к весне", date(2027, 3, 1)),
            (None, "к новому году", date(2027, 1, 1)),
            # «без срока» словами
            (None, "без срока", None),
            (None, "пока не знаю", None),
        ],
    )
    def test_table(self, option_key, text, expected):
        assert deadline.resolve_target_date(option_key, text, today=TODAY) == expected

    @pytest.mark.parametrize(
        ("text", "why"),
        [
            ("вчера", "past"),
            ("1 сентября 2026", "past"),
            ("через 3 года", "too_far"),
            ("01.11.2029", "too_far"),
            ("когда-нибудь потом", "unreadable"),
            ("31 февраля", "unreadable"),
        ],
    )
    def test_refusals_are_named(self, text, why):
        with pytest.raises(deadline.DeadlineError) as exc:
            deadline.resolve_target_date(None, text, today=TODAY)
        assert exc.value.reason == why
        assert exc.value.message  # словами, для 400

    def test_month_end_clamps_instead_of_overflowing(self):
        """31 января + месяц — 28 февраля, а не ошибка и не 3 марта."""
        assert deadline.resolve_target_date(deadline.IN_MONTH, None, today=date(2027, 1, 31)) == date(2027, 2, 28)

    def test_summer_when_today_is_summer_is_next_year(self):
        assert deadline.resolve_target_date(deadline.BY_SUMMER, None, today=date(2027, 7, 10)) == date(2028, 6, 1)

    def test_two_year_ceiling_is_inclusive(self):
        assert deadline.resolve_target_date(None, "20 сентября 2028", today=TODAY) == date(2028, 9, 20)
        with pytest.raises(deadline.DeadlineError):
            deadline.resolve_target_date(None, "21 сентября 2028", today=TODAY)


# ---------------------------------------------------------------------------
# Шаг анкеты ``deadline`` — через API (после слияния К-2 перебазируется)
# ---------------------------------------------------------------------------

from rest_framework.test import APIClient  # noqa: E402

from goals import anketa  # noqa: E402
from goals.models import GoalAnketaAnswer, GoalAnketaRun  # noqa: E402
from services.models import GoalOption  # noqa: E402

VALID_TOKEN = "test-ayla-internal-token-2173"
SELECT_URL = "/api/v1/internal/me/goals/select/"
CTX_URL = "/api/v1/internal/me/decision-context/"


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.GOAL_ANKETA_ENABLED = True


@pytest.fixture
def goal_options(db):
    return [GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=10)]


def _api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = "bot:goals-2173"
    return c


def _answer(api, step: str, **kwargs):
    return api.post(
        SELECT_URL,
        {"answer": {"step": step, **kwargs}, "source_channel": "miniapp"},
        format="json",
    )


def _first_option(step: anketa.AnketaStep) -> dict:
    key = step.options[0][0]
    return {"option_keys": [key]} if step.mode == anketa.MODE_MULTI else {"option_key": key}


def _walk_to_deadline(api):
    """Цель + все сужающие шаги до ``deadline``; возвращает документ с ним в missing."""
    doc = _answer(api, anketa.GOAL_STEP_KEY, option_key="relax").json()["data"]
    for step in anketa.ANKETA_STEPS:
        if step.key == deadline.DEADLINE_STEP_KEY:
            break
        doc = _answer(api, step.key, **_first_option(step)).json()["data"]
    assert doc["missing"][0]["step"] == deadline.DEADLINE_STEP_KEY
    return doc


@pytest.mark.django_db
class TestDeadlineStep:
    def test_step_is_last_optional_and_carries_no_schedule_words(self, customer, token, goal_options):
        """Шаг последний; «без срока» — среди вариантов; сторож C03 (DRF-1751) чист."""
        assert anketa.ANKETA_STEPS[-1].key == deadline.DEADLINE_STEP_KEY
        assert anketa.c03_boundary_violations(anketa.ANKETA_STEPS) == []
        doc = _walk_to_deadline(_api())
        item = doc["missing"][0]
        assert item["progress"]["is_last"] is True
        assert item["allow_free_text"] is True
        assert [o["key"] for o in item["options"]] == ["in_month", "by_summer", "no_deadline"]
        assert item["prompt"].startswith("К какому сроку хочешь? Можно пропустить")

    def test_no_deadline_completes_the_pass_and_leaves_the_goal_without_a_date(
        self, customer, token, goal_options,
    ):
        api = _api()
        _walk_to_deadline(api)
        doc = _answer(api, deadline.DEADLINE_STEP_KEY, option_key=deadline.NO_DEADLINE).json()["data"]

        assert doc["missing"] == []
        assert doc["known"]["goal"]["target_date"] is None
        assert GoalAnketaRun.objects.get(client=customer).completed_at is not None

    def test_free_text_date_lands_on_the_goal(self, customer, token, goal_options):
        api = _api()
        _walk_to_deadline(api)
        resp = _answer(api, deadline.DEADLINE_STEP_KEY, text="до 1 ноября")
        assert resp.status_code == 200, resp.content

        goal = ClientGoal.objects.get(client=customer, state="active")
        assert goal.target_date is not None
        assert (goal.target_date.month, goal.target_date.day) == (11, 1)
        assert resp.json()["data"]["known"]["goal"]["target_date"] == goal.target_date.isoformat()
        # Дословный ввод сохранён как ответ шага (корпус OD-2), не нормализован.
        assert GoalAnketaAnswer.objects.get(
            run__client=customer, step_key=deadline.DEADLINE_STEP_KEY,
        ).answer_text == "до 1 ноября"

    def test_chip_in_month_sets_a_date_a_month_ahead(self, customer, token, goal_options):
        api = _api()
        _walk_to_deadline(api)
        _answer(api, deadline.DEADLINE_STEP_KEY, option_key=deadline.IN_MONTH)
        goal = ClientGoal.objects.get(client=customer, state="active")
        assert goal.target_date == deadline.resolve_target_date(deadline.IN_MONTH, None)

    @pytest.mark.parametrize("text", ["вчера", "1 сентября 2020", "через 5 лет", "когда-нибудь потом"])
    def test_refused_text_is_400_with_words_and_nothing_written(self, customer, token, goal_options, text):
        api = _api()
        _walk_to_deadline(api)
        resp = _answer(api, deadline.DEADLINE_STEP_KEY, text=text)

        assert resp.status_code == 400, resp.content
        body = resp.json()
        words = str(body)
        assert "срок" in words.casefold() or "дат" in words.casefold()  # словами, не кодом
        assert not GoalAnketaAnswer.objects.filter(
            run__client=customer, step_key=deadline.DEADLINE_STEP_KEY,
        ).exists()
        assert GoalAnketaRun.objects.get(client=customer).completed_at is None
        assert ClientGoal.objects.get(client=customer, state="active").target_date is None

    def test_second_pass_changes_the_date(self, customer, token, goal_options):
        """Правка срока — повторный проход («Пройти анкету заново» / «Изменить»):
        шаг спрашивается снова, подтверждение или новый ответ — без отдельной ручки."""
        api = _api()
        _walk_to_deadline(api)
        _answer(api, deadline.DEADLINE_STEP_KEY, option_key=deadline.NO_DEADLINE)
        assert ClientGoal.objects.get(client=customer, state="active").target_date is None

        resp = api.post(SELECT_URL, {"intent": "start_anketa", "source_channel": "miniapp"}, format="json")
        assert resp.status_code == 200, resp.content
        doc = _answer(api, anketa.GOAL_STEP_KEY, option_key="relax").json()["data"]
        while doc["missing"][0]["step"] != deadline.DEADLINE_STEP_KEY:
            item = doc["missing"][0]
            resp = _answer(api, item["step"], confirm=True)
            assert resp.status_code == 200, resp.content
            doc = resp.json()["data"]
        item = doc["missing"][0]
        assert item["mode"] == anketa.MODE_CONFIRM  # прошлый ответ «без срока» известен
        assert item["known_value"]["option_key"] == deadline.NO_DEADLINE

        resp = _answer(api, deadline.DEADLINE_STEP_KEY, text="до 1 ноября")
        assert resp.status_code == 200, resp.content
        goal = ClientGoal.objects.get(client=customer, state="active")
        assert (goal.target_date.month, goal.target_date.day) == (11, 1)

    def test_revise_inside_an_open_pass_is_refused_for_the_closing_step(self, customer, token, goal_options):
        """Ответ на deadline закрывает проход — «Изменить» в нём же невозможно (409, как у любого
        закрытого прохода); правка — повторным проходом (узел выше)."""
        api = _api()
        _walk_to_deadline(api)
        _answer(api, deadline.DEADLINE_STEP_KEY, option_key=deadline.NO_DEADLINE)
        resp = _answer(api, deadline.DEADLINE_STEP_KEY, text="до 1 ноября", revise=True)
        assert resp.status_code == 409

    def test_goal_text_survives_the_deadline_answer(self, customer, token):
        """§48 «оставь как в чате»: свободная цель + срок — текст цели дословен."""
        api = _api()
        doc = _answer(api, anketa.GOAL_STEP_KEY, text="хочу похудеть к отпуску").json()["data"]
        while doc["missing"] and doc["missing"][0]["step"] != deadline.DEADLINE_STEP_KEY:
            item = doc["missing"][0]
            step = anketa.narrowing_step(item["step"])
            if step is None:
                pytest.skip("проход ушёл в уточнение цели — не про срок")
            doc = _answer(api, step.key, **_first_option(step)).json()["data"]
        if not doc["missing"]:
            pytest.skip("проход завершился без шага срока")
        _answer(api, deadline.DEADLINE_STEP_KEY, text="через месяц")
        goal = ClientGoal.objects.get(client=customer, state="active")
        assert goal.goal_text == "хочу похудеть к отпуску"
        assert goal.target_date is not None
