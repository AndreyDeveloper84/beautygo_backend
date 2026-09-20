"""Направление под цель и собранные ответы в документе — DRF-1772 (К-3).

Что заперто:

1. ``known.goal.direction`` — из курируемой таблицы ``GoalDirection``:
   строка «цель × область» старше запасной «цель, ''»; неактивная не
   считается; таблица пустая → ``None`` (карточки не будет — C04.4);
2. цель свободным текстом (без ключа) направления не имеет — ``None``;
3. ``known.goal.answers`` — ответы завершённого прохода ЭТОЙ цели в форме
   «Уже учла» (после закрытия прохода ``known.anketa`` пуст — а причины
   карточке нужны); повторный проход перезаписывает картину; чужой цели
   ответы не приписываются;
4. «Не знаю» на области → запасная строка под цель, не подбор по близости.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals import anketa
from goals.decision_context import build_decision_context
from goals.direction import answers_for_goal, direction_for
from goals.models import ClientGoal
from services.models import GoalDirection, GoalOption
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-1772"
SELECT_URL = "/api/v1/internal/me/goals/select/"
EXTERNAL_USER_ID = "bot:direction"

AREA, FEELING = anketa.ANKETA_STEPS[0], anketa.ANKETA_STEPS[1]


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995001772", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.GOAL_ANKETA_ENABLED = True


@pytest.fixture
def self_care(db):
    return GoalOption.objects.create(key="self_care", label="Привести себя в порядок", sort_order=10)


@pytest.fixture
def directions_single(self_care):
    GoalDirection.objects.create(
        goal_option=self_care, area_key="", what="Общее направление", subline="Под цель целиком",
    )


@pytest.fixture
def directions(self_care):
    GoalDirection.objects.create(
        goal_option=self_care, area_key="", what="Общее направление", subline="Под цель целиком",
    )
    GoalDirection.objects.create(
        goal_option=self_care, area_key="face", what="Направление для лица", subline="Про лицо",
    )
    GoalDirection.objects.create(
        goal_option=self_care, area_key="hair", what="Выключено", is_active=False,
    )


def _api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


def _answer(api, step: str, **kwargs):
    resp = api.post(
        SELECT_URL, {"answer": {"step": step, **kwargs}, "source_channel": "miniapp"}, format="json",
    )
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _pick(api, key="self_care"):
    resp = api.post(SELECT_URL, {"goal_key": key, "source_channel": "miniapp"}, format="json")
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _collect(api, area="face"):
    _answer(api, AREA.key, option_key=area)
    _answer(api, FEELING.key, option_keys=[FEELING.options[0][0]])
    # DRF-2173: проход закрывает необязательный шаг срока («без срока»).
    return _answer(api, "deadline", option_key="no_deadline")


@pytest.mark.django_db
class TestDirection:
    def test_area_row_wins_over_the_goal_fallback(self, customer, token, directions):
        api = _api()
        _pick(api)
        doc = _collect(api, area="face")
        direction = doc["known"]["goal"]["direction"]
        assert direction == {"what": "Направление для лица", "subline": "Про лицо", "area_key": "face"}

    def test_unknown_area_falls_back_to_the_goal_row(self, customer, token, directions):
        api = _api()
        _pick(api)
        doc = _collect(api, area="hands")  # строки под hands нет
        assert doc["known"]["goal"]["direction"]["what"] == "Общее направление"
        assert doc["known"]["goal"]["direction"]["area_key"] is None

    def test_inactive_row_does_not_count(self, customer, token, directions):
        api = _api()
        _pick(api)
        doc = _collect(api, area="hair")
        assert doc["known"]["goal"]["direction"]["what"] == "Общее направление"

    def test_empty_table_means_no_direction(self, customer, token, self_care):
        api = _api()
        _pick(api)
        doc = _collect(api)
        assert doc["known"]["goal"]["label"] == "Привести себя в порядок"
        assert doc["known"]["goal"]["direction"] is None

    def test_free_text_goal_has_no_direction(self, customer, directions):
        goal = ClientGoal.objects.create(client=customer, goal_text="хочу маникюр", source_channel="bot")
        assert direction_for(goal) is None

    def test_dont_know_on_area_uses_the_goal_row(self, customer, token, directions):
        api = _api()
        _pick(api)
        _answer(api, AREA.key, option_key=anketa.UNKNOWN_OPTION_KEY)
        _answer(api, FEELING.key, option_keys=[FEELING.options[0][0]])
        doc = _answer(api, "deadline", option_key="no_deadline")  # DRF-2173: закрывает проход
        assert doc["known"]["goal"]["direction"]["what"] == "Общее направление"

    def test_direction_is_present_before_answers_too(self, customer, token, directions):
        """Направление под цель есть с момента выбора цели (запасная строка);
        карточку строит бот и только на собранном контексте — это его гейт."""
        doc = _pick(_api())
        assert doc["known"]["goal"]["direction"]["what"] == "Общее направление"
        assert doc["known"]["goal"]["answers"] == []


@pytest.mark.django_db
class TestAlternatives:
    """C04.3 (DRF-1770): основной вариант + до двух других подходов.

    Альтернативы — ТЕ ЖЕ курируемые строки под цель (другие области и
    запасная); кодом ничего не придумывается, и на пустой таблице их нет,
    как нет и направления.
    """

    def test_primary_first_then_up_to_two_others(self, customer, token, self_care):
        GoalDirection.objects.create(
            goal_option=self_care, area_key="", what="Общее", sort_order=30
        )
        GoalDirection.objects.create(
            goal_option=self_care, area_key="face", what="Лицо", sort_order=10
        )
        GoalDirection.objects.create(
            goal_option=self_care, area_key="body", what="Тело", sort_order=20
        )
        GoalDirection.objects.create(
            goal_option=self_care, area_key="hair", what="Волосы", sort_order=40
        )
        api = _api()
        _pick(api)
        doc = _collect(api, area="face")

        goal = doc["known"]["goal"]
        assert goal["direction"]["what"] == "Лицо"  # основной — под область
        assert [d["what"] for d in goal["directions"]] == ["Лицо", "Тело", "Общее"]
        assert len(goal["directions"]) == 3  # primary + ровно две

    def test_a_single_row_gives_one_element(self, customer, token, directions_single):
        api = _api()
        _pick(api)
        doc = _collect(api)
        goal = doc["known"]["goal"]
        assert goal["direction"]["what"] == "Общее направление"
        assert [d["what"] for d in goal["directions"]] == ["Общее направление"]

    def test_empty_table_has_neither(self, customer, token, self_care):
        api = _api()
        _pick(api)
        doc = _collect(api)
        goal = doc["known"]["goal"]
        assert goal["label"] == "Привести себя в порядок"  # присутствие: цель есть
        assert goal["direction"] is None
        assert goal["directions"] == []

    def test_inactive_rows_are_not_alternatives(self, customer, token, self_care):
        GoalDirection.objects.create(goal_option=self_care, area_key="", what="Общее")
        GoalDirection.objects.create(
            goal_option=self_care, area_key="body", what="Выключено", is_active=False
        )
        api = _api()
        _pick(api)
        doc = _collect(api, area="hands")
        assert [d["what"] for d in doc["known"]["goal"]["directions"]] == ["Общее"]


@pytest.mark.django_db
class TestCollectedAnswers:
    def test_answers_of_the_completed_run_travel_with_the_goal(self, customer, token, self_care):
        api = _api()
        _pick(api)
        doc = _collect(api, area="face")
        answers = doc["known"]["goal"]["answers"]
        assert [a["step"] for a in answers] == [AREA.key, FEELING.key, "deadline"]
        assert answers[0]["label"] == "Лицо и кожа"
        assert answers[0]["origin"] == anketa.ORIGIN_ANKETA
        # Открытого прохода нет — «Уже учла» пуст, а причины карточке есть откуда взять.
        assert doc["known"]["anketa"] == []

    def test_repeat_pass_rewrites_the_picture(self, customer, token, self_care):
        api = _api()
        _pick(api)
        _collect(api, area="face")
        api.post(SELECT_URL, {"intent": "start_anketa", "source_channel": "miniapp"}, format="json")
        _answer(api, anketa.GOAL_STEP_KEY, option_key="self_care")
        doc = _collect(api, area="hands")
        assert doc["known"]["goal"]["answers"][0]["label"] == "Руки и ногти"

    def test_answers_are_not_borrowed_from_another_goal(self, customer, token, self_care):
        GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=20)
        api = _api()
        _pick(api, "self_care")
        _collect(api, area="face")
        doc = _pick(api, "relax")
        assert doc["known"]["goal"]["goal_key"] == "relax"
        assert doc["known"]["goal"]["answers"] == []

    def test_helper_returns_empty_without_a_completed_run(self, customer, self_care):
        goal = ClientGoal.objects.create(client=customer, goal_key="self_care", source_channel="bot")
        assert answers_for_goal(goal) == []
        assert build_decision_context(customer)["known"]["goal"]["answers"] == []
