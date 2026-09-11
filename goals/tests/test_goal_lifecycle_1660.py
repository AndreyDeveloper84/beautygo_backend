"""DRF-1660 — жизненный цикл цели: четыре состояния вместо ``is_active``.

Решение владельца §97 OD-GOAL-B (10.09.2026): ``ACTIVE / PAUSED /
ACHIEVED / ARCHIVED``; человек обязан уметь приостановить, завершить и
архивировать цель **без создания замещающей**; 33 закрытых ``is_active=False``
автоматически не толкуются. Основание запрета прежней модели — §122:
состояние, из которого нет выхода.

Что здесь проверяется:

* таблица переходов — и разрешённые, и отказанные, по имени кода;
* каждый вход в состояние — без замещающей цели (главное требование);
* ``SUPERSEDED`` пишет только выбор новой цели, ``LEGACY_*`` — только
  миграция; ни одно из них нельзя запросить через API;
* документ состояния показывает цель на паузе с ``id`` — у паузы есть
  выход (§122);
* миграция ``0006``: закрытые строки едут в ``LEGACY_INACTIVE_REASON_UNKNOWN``,
  а не в правдоподобное; открытые — в ``ACTIVE``; обратный ход без потерь.

Что проверяется в ``test_goal_wiring_od1.py``: цель на паузе не режет
выдачу — это стража на экспозицию, она живёт рядом с фикстурами полок.
"""
from __future__ import annotations

import inspect

import pytest
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from rest_framework.test import APIClient

from goals import lifecycle
from goals.lifecycle import (
    ALLOWED_TRANSITIONS,
    GOAL_ANOTHER_ACTIVE,
    GOAL_TRANSITION_NOT_ALLOWED,
    OPEN_STATES,
    REQUESTABLE_STATES,
    GoalTransitionRefused,
    transition,
)
from goals.models import ClientGoal
from users.models import User

State = ClientGoal.State

VALID_TOKEN = "test-ayla-internal-token-1660"
EXTERNAL_USER_ID = "bot:goals-1660"
SELECT_URL = "/api/v1/internal/me/goals/select/"
STATE_URL = "/api/v1/internal/me/goals/state/"
CTX_URL = "/api/v1/internal/me/decision-context/"


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995016600", is_proxy=True,
    )


@pytest.fixture
def stranger(db):
    return User.objects.create_user(
        username="bot:goals-1660-other", password="x", role="client",
        phone="+79995016601", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


def _api(external_user_id: str = EXTERNAL_USER_ID) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _goal(client, *, state: str = State.ACTIVE, key: str = "relax") -> ClientGoal:
    return ClientGoal.objects.create(
        client=client, goal_key=key, source_channel="bot", state=state,
    )


# ---------------------------------------------------------------------------
# Таблица переходов
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTransitionTable:
    def test_every_state_is_in_the_table(self):
        """Новое состояние без строки в таблице — это состояние без выхода."""
        assert set(ALLOWED_TRANSITIONS) == {value for value, _ in State.choices}

    def test_open_states_have_an_exit(self):
        """§122: из ACTIVE и PAUSED есть выход, и выход — переход, а не новая цель."""
        for state in OPEN_STATES:
            assert ALLOWED_TRANSITIONS[state], f"{state}: выхода нет"

    def test_paused_can_come_back_to_active(self):
        assert State.ACTIVE in ALLOWED_TRANSITIONS[State.PAUSED]

    @pytest.mark.parametrize("to_state", sorted(REQUESTABLE_STATES - {State.ACTIVE}))
    def test_active_goal_leaves_active_without_a_replacement(self, customer, to_state):
        goal = _goal(customer)

        transition(goal, to_state)

        goal.refresh_from_db()
        assert goal.state == to_state
        assert goal.state_changed_at is not None
        # Главное: замещающей цели не появилось, ACTIVE у клиента нет.
        assert ClientGoal.objects.filter(client=customer).count() == 1
        assert not ClientGoal.objects.filter(client=customer, state=State.ACTIVE).exists()

    def test_paused_resumes(self, customer):
        goal = _goal(customer, state=State.PAUSED)
        transition(goal, State.ACTIVE)
        goal.refresh_from_db()
        assert goal.state == State.ACTIVE

    @pytest.mark.parametrize(
        "terminal",
        [State.ACHIEVED, State.ARCHIVED, State.SUPERSEDED, State.LEGACY_INACTIVE_REASON_UNKNOWN],
    )
    @pytest.mark.parametrize("to_state", sorted(REQUESTABLE_STATES))
    def test_terminal_states_refuse_by_name(self, customer, terminal, to_state):
        if terminal == to_state:
            pytest.skip("идемпотентность проверяется отдельно")
        goal = _goal(customer, state=terminal)

        with pytest.raises(GoalTransitionRefused) as exc:
            transition(goal, to_state)

        assert exc.value.code == GOAL_TRANSITION_NOT_ALLOWED
        goal.refresh_from_db()
        assert goal.state == terminal, "отказ ничего не записал"

    def test_same_state_is_idempotent_and_does_not_move_the_clock(self, customer):
        goal = _goal(customer, state=State.PAUSED)
        goal.state_changed_at = None
        goal.save(update_fields=["state_changed_at"])

        transition(goal, State.PAUSED)

        goal.refresh_from_db()
        assert goal.state == State.PAUSED
        assert goal.state_changed_at is None

    def test_resume_refuses_while_another_goal_is_active(self, customer):
        """Схема держит «одна ACTIVE на клиента»: отказ, а не тихое закрытие соседа."""
        active = _goal(customer, key="relax")
        paused = _goal(customer, key="sleep", state=State.PAUSED)

        with pytest.raises(GoalTransitionRefused) as exc:
            transition(paused, State.ACTIVE)

        assert exc.value.code == GOAL_ANOTHER_ACTIVE
        active.refresh_from_db()
        paused.refresh_from_db()
        assert active.state == State.ACTIVE, "сосед не тронут"
        assert paused.state == State.PAUSED

    def test_legacy_is_written_by_nobody_but_the_migration(self):
        """В коде нет писателя LEGACY_*: смотрим исходник модуля, не память."""
        src = inspect.getsource(lifecycle)
        assert "update(state=State.LEGACY" not in src
        assert "state = State.LEGACY" not in src


# ---------------------------------------------------------------------------
# Выбор новой цели — единственный писатель SUPERSEDED
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestSelectSupersedes:
    def test_new_selection_supersedes_active_and_leaves_paused_alone(self, token, customer):
        active = _goal(customer, key="relax")
        paused = _goal(customer, key="sleep", state=State.PAUSED)

        r = _api().post(SELECT_URL, {"goal_text": "новая", "source_channel": "bot"}, format="json")
        assert r.status_code == 200, r.data

        active.refresh_from_db()
        paused.refresh_from_db()
        assert active.state == State.SUPERSEDED
        assert active.state_changed_at is not None
        assert paused.state == State.PAUSED, "пауза — не ACTIVE, ограничению не мешает"
        assert ClientGoal.objects.filter(client=customer, state=State.ACTIVE).count() == 1

    def test_one_active_per_client_is_enforced_on_state(self, customer):
        _goal(customer)
        with pytest.raises(IntegrityError):
            _goal(customer, key="other")

    def test_two_paused_goals_are_allowed(self, customer):
        """Ограничение — про ACTIVE, а не про «открытые»."""
        _goal(customer, key="a", state=State.PAUSED)
        _goal(customer, key="b", state=State.PAUSED)
        assert ClientGoal.objects.filter(client=customer, state=State.PAUSED).count() == 2


# ---------------------------------------------------------------------------
# API: POST /internal/me/goals/state/
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGoalStateEndpoint:
    def test_pause_returns_document_without_active_goal_but_with_the_paused_one(
        self, token, customer,
    ):
        goal = _goal(customer)

        r = _api().post(STATE_URL, {"goal_id": str(goal.id), "state": "paused"}, format="json")

        assert r.status_code == 200, r.data
        doc = r.json()["data"]
        assert doc["known"]["goal"] is None, "на паузе — не ведём"
        # …но цель не исчезла: у неё есть адрес и состояние — есть выход.
        assert [(g["id"], g["state"]) for g in doc["known"]["goals"]] == [
            (str(goal.id), "paused"),
        ]
        goal.refresh_from_db()
        assert goal.state == State.PAUSED
        assert ClientGoal.objects.filter(client=customer).count() == 1

    def test_resume_puts_the_goal_back_into_known_goal(self, token, customer):
        goal = _goal(customer, state=State.PAUSED)

        r = _api().post(STATE_URL, {"goal_id": str(goal.id), "state": "active"}, format="json")

        assert r.status_code == 200, r.data
        doc = r.json()["data"]
        assert doc["known"]["goal"]["id"] == str(goal.id)
        assert doc["known"]["goal"]["state"] == "active"

    @pytest.mark.parametrize("state", ["achieved", "archived"])
    def test_finish_without_a_replacement(self, token, customer, state):
        goal = _goal(customer)

        r = _api().post(STATE_URL, {"goal_id": str(goal.id), "state": state}, format="json")

        assert r.status_code == 200, r.data
        doc = r.json()["data"]
        assert doc["known"]["goal"] is None
        assert doc["known"]["goals"] == [], "терминальная цель в открытых не показывается"
        assert ClientGoal.objects.filter(client=customer).count() == 1

    def test_decision_context_lists_paused_goals(self, token, customer):
        _goal(customer, key="a", state=State.PAUSED)
        _goal(customer, key="b", state=State.ARCHIVED)

        doc = _api().get(CTX_URL).json()["data"]

        assert doc["known"]["goal"] is None
        assert [g["goal_key"] for g in doc["known"]["goals"]] == ["a"]

    def test_transition_not_allowed_is_409_with_its_code(self, token, customer):
        goal = _goal(customer, state=State.ARCHIVED)

        r = _api().post(STATE_URL, {"goal_id": str(goal.id), "state": "active"}, format="json")

        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == GOAL_TRANSITION_NOT_ALLOWED
        assert err["details"] == {"from_state": "archived", "to_state": "active"}

    def test_another_active_is_409_with_its_own_code(self, token, customer):
        _goal(customer, key="relax")
        paused = _goal(customer, key="sleep", state=State.PAUSED)

        r = _api().post(STATE_URL, {"goal_id": str(paused.id), "state": "active"}, format="json")

        assert r.status_code == 409
        assert r.json()["error"]["code"] == GOAL_ANOTHER_ACTIVE

    @pytest.mark.parametrize("state", ["superseded", "legacy_inactive_reason_unknown", "deleted"])
    def test_non_requestable_states_are_400(self, token, customer, state):
        goal = _goal(customer)

        r = _api().post(STATE_URL, {"goal_id": str(goal.id), "state": state}, format="json")

        assert r.status_code == 400
        goal.refresh_from_db()
        assert goal.state == State.ACTIVE

    def test_someone_elses_goal_is_404_not_409(self, token, customer, stranger):
        """Чужой id — «не найдено»: по коду нельзя узнать, что чужая цель есть."""
        foreign = _goal(stranger)

        r = _api().post(STATE_URL, {"goal_id": str(foreign.id), "state": "paused"}, format="json")

        assert r.status_code == 404
        foreign.refresh_from_db()
        assert foreign.state == State.ACTIVE

    def test_goal_id_is_required(self, token, customer):
        _goal(customer)
        r = _api().post(STATE_URL, {"state": "paused"}, format="json")
        assert r.status_code == 400

    def test_without_bearer_is_refused(self, customer):
        goal = _goal(customer)
        c = APIClient()
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
        r = c.post(STATE_URL, {"goal_id": str(goal.id), "state": "paused"}, format="json")
        assert r.status_code in (401, 403)
        goal.refresh_from_db()
        assert goal.state == State.ACTIVE


# ---------------------------------------------------------------------------
# Миграция 0006: без потери и без догадок
# ---------------------------------------------------------------------------

postgres_only = pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="MigrationExecutor с partial-UniqueConstraint — только на Postgres, как в CI",
)


@postgres_only
@pytest.mark.django_db(transaction=True)
class TestStateMigration:
    @pytest.fixture(autouse=True)
    def _restore_goals_schema(self):
        """Вернуть ``goals`` на лист графа после теста.

        ``transaction=True``: DDL коммитится в тестовую базу, и без этого
        teardown следующий тест увидел бы ``ClientGoal`` с ``is_active``
        (см. payments/tests/test_table_rename_migration.py — это уже
        ломало CI однажды).
        """
        yield
        executor = MigrationExecutor(connection)
        leaves = executor.loader.graph.leaf_nodes("goals")
        if leaves:
            executor.migrate(leaves)

    @staticmethod
    def _migrate(target: str):
        executor = MigrationExecutor(connection)
        executor.migrate([("goals", target)])
        return executor.loader.project_state(("goals", target)).apps

    def test_closed_rows_become_legacy_unknown_open_rows_become_active(self):
        apps_before = self._migrate("0005_remove_clientgoal_tenant")
        OldGoal = apps_before.get_model("goals", "ClientGoal")
        # Пользователь — живой моделью: историческая ``users.User`` на
        # состоянии goals.0005 не знает колонок, которые в таблице уже
        # есть (NOT NULL без умолчания). Связь — по id, потому что
        # исторический FK не принимает экземпляр живого класса.
        user = User.objects.create_user(
            username="mig-1660", password="x", phone="+79995016699", role="client",
        )
        # 3 закрытых + 1 открытая: число закрытых берётся из данных, не из кода.
        closed_ids = [
            OldGoal.objects.create(
                client_id=user.id, goal_key=f"old-{i}", source_channel="bot", is_active=False,
            ).id
            for i in range(3)
        ]
        open_id = OldGoal.objects.create(
            client_id=user.id, goal_key="now", source_channel="bot", is_active=True,
        ).id

        apps_after = self._migrate("0006_clientgoal_state_lifecycle")
        NewGoal = apps_after.get_model("goals", "ClientGoal")

        states = dict(NewGoal.objects.values_list("id", "state"))
        assert {states[i] for i in closed_ids} == {"legacy_inactive_reason_unknown"}
        assert states[open_id] == "active"
        # Ни одна строка не потеряна и ни одна не угадана.
        assert NewGoal.objects.count() == 4
        assert not NewGoal.objects.filter(state__in=["achieved", "archived"]).exists()
        assert NewGoal.objects.filter(state_changed_at__isnull=False).count() == 0, (
            "момент закрытия старых строк неизвестен — NULL, не now()"
        )
        assert not any(f.name == "is_active" for f in NewGoal._meta.get_fields())

    def test_backwards_restores_the_bool_without_loss(self):
        apps_after = self._migrate("0006_clientgoal_state_lifecycle")
        NewGoal = apps_after.get_model("goals", "ClientGoal")
        user = User.objects.create_user(
            username="mig-1660-b", password="x", phone="+79995016698", role="client",
        )
        ids = {
            state: NewGoal.objects.create(
                client_id=user.id, goal_key=f"g-{state}", source_channel="bot", state=state,
            ).id
            for state in [
                "paused", "achieved", "archived", "superseded",
                "legacy_inactive_reason_unknown",
            ]
        }
        ids["active"] = NewGoal.objects.create(
            client_id=user.id, goal_key="g-active", source_channel="bot", state="active",
        ).id

        apps_before = self._migrate("0005_remove_clientgoal_tenant")
        OldGoal = apps_before.get_model("goals", "ClientGoal")

        flags = dict(OldGoal.objects.values_list("id", "is_active"))
        assert flags[ids["active"]] is True
        assert all(flags[ids[s]] is False for s in ids if s != "active")
