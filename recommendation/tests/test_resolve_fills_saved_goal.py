"""Ручка резолвера для бота подставляет сохранённую цель человека.

Домашняя полка мини-приложения (бот, ``build_shelf_request(goal_key=None)``)
шлёт ``need.origin=GOAL`` без ключа. До этой правки ручка
``internal/recommendation/resolve/`` брала нужду только из тела: источник
получал ``goal_key=None`` — фильтра по цели нет, а S2 объявлялась «нужда не
названа». Человек выбрал «расслабиться» — полка мини-приложения подбирала
так, будто цели нет. При этом путь приложения каталога
(``users/catalog_recommendations_api``) тому же человеку его цель
подставлял: две поверхности одного человека расходились.

Узлы держат правило с обеих сторон:

* сохранённая цель доезжает до источника, и S2 становится активной;
* выключенный флаг, отсутствие цели — как раньше: ключа нет;
* названный в теле ключ не перекрывается сохранённым;
* другие происхождения нужды (явные слова) не трогаются;
* путь приложения отвечает по-прежнему — читатель у обоих путей один.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from goals.models import ClientGoal
from services.models import GoalOption, GoalOptionCategory, ServiceCategory
from users.models import User

from .conftest import make_facts

VALID_TOKEN = "test-ayla-internal-token-saved-goal"
URL = "/api/v1/internal/recommendation/resolve/"
EXTERNAL_USER_ID = "bot:saved-goal"

#: Что увидел источник: нужда, с которой резолвер к нему пришёл.
_SEEN_NEEDS: list = []


class _RecordingSource:
    def fetch(self, *, scope, need):  # noqa: ARG002 - порт шире, чем нужно заглушке
        _SEEN_NEEDS.append(need)
        return [make_facts(), make_facts()]


def recording_source_factory(*, viewer=None):  # noqa: ARG001 - подпись фабрики по контракту
    return _RecordingSource()


@pytest.fixture(autouse=True)
def _bound(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.RECOMMENDATION_CANDIDATE_SOURCE = (
        "recommendation.tests.test_resolve_fills_saved_goal.recording_source_factory"
    )
    _SEEN_NEEDS.clear()
    yield
    _SEEN_NEEDS.clear()


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79994100077", is_proxy=True,
    )


@pytest.fixture
def relax(db):
    """Курируемая цель на корневой категории — как на пилоте; разрешается."""
    root = ServiceCategory.objects.create(name="Массаж тела", slug="saved-goal-massage")
    option = GoalOption.objects.create(key="relax", label="Расслабиться и снять стресс")
    GoalOptionCategory.objects.create(goal_option=option, category=root, sort_order=0)
    return option


@pytest.fixture
def recharge(db):
    """Вторая разрешимая цель — чтобы выбор между целями было видно."""
    root = ServiceCategory.objects.create(name="SPA", slug="saved-goal-spa")
    option = GoalOption.objects.create(key="recharge", label="Восстановиться")
    GoalOptionCategory.objects.create(goal_option=option, category=root, sort_order=0)
    return option


def _save_goal(
    user, key="relax", *, minutes_ago: int = 0, state: str = ClientGoal.State.ACTIVE,
) -> ClientGoal:
    # Активная цель у человека одна (`clientgoal_one_active_per_client`),
    # поэтому неактивная заводится сразу в своём состоянии.
    goal = ClientGoal.objects.create(
        client=user, goal_key=key, source_channel=ClientGoal.SourceChannel.BOT, state=state,
    )
    if minutes_ago:
        ClientGoal.objects.filter(pk=goal.pk).update(
            selected_at=timezone.now() - timedelta(minutes=minutes_ago)
        )
    return goal


def _resolve(need: dict) -> dict:
    api = APIClient()
    api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    api.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    response = api.post(
        URL,
        {
            "request_id": "req-saved-goal",
            "surface": "MINIAPP_HOME",
            "scope": {"mode": "MARKETPLACE"},
            "need": need,
            "safety_state": "NOT_APPLICABLE",
            "k": 3,
        },
        format="json",
    )
    assert response.status_code == 200, response.data
    return response.data["data"]


#: Форма нужды, которую сегодня шлёт полка мини-приложения (бот,
#: ``apps/marketplace/resolver_request.build_shelf_request(goal_key=None)``).
SHELF_NEED = {"origin": "GOAL", "capability_refs": [], "canonical_service_refs": [], "goal_key": None}


@pytest.mark.django_db
class TestTheShelfGetsThePersonsGoal:
    def test_the_saved_goal_reaches_the_source(self, settings, customer, relax):
        settings.GOAL_RESOLUTION_ENABLED = True
        _save_goal(customer)

        _resolve(SHELF_NEED)

        assert [n.goal_key for n in _SEEN_NEEDS] == ["relax"]
        assert _SEEN_NEEDS[0].is_stated, "с ключом цели S2 обязана различать, а не молчать"

    def test_switch_off_keeps_todays_answer(self, settings, customer, relax):
        settings.GOAL_RESOLUTION_ENABLED = False
        _save_goal(customer)

        _resolve(SHELF_NEED)

        assert [n.goal_key for n in _SEEN_NEEDS] == [None]

    def test_no_saved_goal_keeps_todays_answer(self, settings, customer, relax):
        settings.GOAL_RESOLUTION_ENABLED = True

        _resolve(SHELF_NEED)

        assert [n.goal_key for n in _SEEN_NEEDS] == [None]
        assert not _SEEN_NEEDS[0].is_stated

    def test_a_key_named_in_the_body_is_not_overridden(self, settings, customer, relax):
        settings.GOAL_RESOLUTION_ENABLED = True
        _save_goal(customer)

        _resolve({**SHELF_NEED, "goal_key": "recharge"})

        assert [n.goal_key for n in _SEEN_NEEDS] == ["recharge"]

    def test_explicit_words_are_not_turned_into_a_goal(self, settings, customer, relax):
        settings.GOAL_RESOLUTION_ENABLED = True
        _save_goal(customer)

        _resolve({"origin": "USER_EXPLICIT", "raw_text": "массаж"})

        assert [(n.goal_key, n.raw_text) for n in _SEEN_NEEDS] == [(None, "массаж")]

    def test_a_goal_that_is_no_longer_active_is_not_used(self, settings, customer, relax):
        settings.GOAL_RESOLUTION_ENABLED = True
        goal = _save_goal(customer)
        ClientGoal.objects.filter(pk=goal.pk).update(state=ClientGoal.State.PAUSED)

        _resolve(SHELF_NEED)

        assert [n.goal_key for n in _SEEN_NEEDS] == [None]

    def test_a_newer_inactive_goal_does_not_shadow_the_active_one(
        self, settings, customer, relax, recharge,
    ):
        """Свежая, но поставленная на паузу цель не перекрывает действующую.

        Без этого узла фильтр состояния в выборке ничего не стерёг: при
        одной неактивной цели ответ «нет цели» давала уже проверка флага.
        """
        settings.GOAL_RESOLUTION_ENABLED = True
        _save_goal(customer, "recharge", minutes_ago=60)
        _save_goal(customer, "relax", state=ClientGoal.State.PAUSED)

        _resolve(SHELF_NEED)

        assert [n.goal_key for n in _SEEN_NEEDS] == ["recharge"]

    def test_a_newer_goal_of_another_person_does_not_win(
        self, settings, customer, relax, recharge,
    ):
        settings.GOAL_RESOLUTION_ENABLED = True
        _save_goal(customer, "recharge", minutes_ago=60)
        other = User.objects.create_user(
            username="bot:newer-other", password="x", role="client",
            phone="+79994100079", is_proxy=True,
        )
        _save_goal(other, "relax")

        _resolve(SHELF_NEED)

        assert [n.goal_key for n in _SEEN_NEEDS] == ["recharge"]

    def test_another_persons_goal_is_not_used(self, settings, customer, relax):
        """Кого спрашивают — говорит аутентификация, а не чья цель есть в базе."""
        settings.GOAL_RESOLUTION_ENABLED = True
        other = User.objects.create_user(
            username="bot:someone-else", password="x", role="client",
            phone="+79994100078", is_proxy=True,
        )
        _save_goal(other)

        _resolve(SHELF_NEED)

        assert [n.goal_key for n in _SEEN_NEEDS] == [None]


@pytest.mark.django_db
def test_both_surfaces_read_the_goal_through_one_reader():
    """Путь приложения и ручка резолвера — один читатель сохранённой цели.

    Две копии одного правила разъехались бы молча; ровно так полка
    мини-приложения и осталась без цели.
    """
    import inspect

    import recommendation.views as resolver_view
    import users.catalog_recommendations_api as app_path

    assert "saved_goal_key_for" in inspect.getsource(resolver_view)
    assert "saved_goal_key_for(request.user)" in inspect.getsource(app_path)
    assert not hasattr(app_path, "_saved_goal_key"), "вторая копия правила вернулась"
