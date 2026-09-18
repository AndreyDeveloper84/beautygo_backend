"""Plan Lite без веса — писатель, чтение, флаг, сторожа (DRF-2101, §49).

Решение владельца 18.09: план из 1–3 обязательств-ДЕЙСТВИЙ из уже данной
цели (``goals.ClientGoal``), только adherence «N из M» за текущее ведро
каденса, ни одного наблюдения тела. Гейт O (§152) к этому пути не
относится — потому что нечего хранить; и это доказывается сторожами, а не
словами:

* **перепись импортов** — модули ``wellness/plan_lite*.py`` не импортируют
  ``body_observation_gate`` / ``record_observation`` / ``ProgressObservation``
  / ``NutritionProfile`` / ``weight_kg``; нижняя граница — модулей ≥ 2 и
  они импортируют ``PersonalPlan``; ложный вход — строка импорта,
  подсунутая сканеру, обязана его покраснить;
* **поведение** — создание плана не пишет ни одной ``ProgressObservation``
  при созданном ``PersonalPlan`` (присутствие раньше отсутствия);
* **В-5 (DRF-1332)** — в ответе только факты действий: множество ключей
  ``plan_lite`` и ``actions`` ⊆ разрешённому; ни процента, ни «achieved».

Цель держится самим планом: ``PersonalPlan.goal`` → ``ClientGoal`` +
``goal_key`` снимком (вариант (б) главного окна: DesiredOutcome через
``record_outcome`` заперт гейтом D, а обход гейта не санкционирован §49).

Флаг ``PLAN_LITE_ENABLED`` (default false): писатель — 404
``PLAN_LITE_DISABLED`` по замыслу, НЕ 5xx (общий breaker бота считает
постоянный 5xx аварией); чтение — ``plan_lite: null``.

Красное до правки объявлено поимённо до прогона: 12 красных / 0 зелёных
«до» (маршрута, поля и модулей нет).
"""
from __future__ import annotations

import ast
from datetime import timedelta
from pathlib import Path

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from goals.models import ClientGoal
from nutrition.models import FoodLog, WaterEntry
from users.models import User
from wellness.models import PersonalPlan, PlanAction, ProgressObservation

pytestmark = pytest.mark.django_db

VALID_TOKEN = "test-ayla-internal-token-plan-lite"
PLAN_URL = "/api/v1/internal/me/plan-lite/"
CTX_URL = "/api/v1/internal/me/wellness-context/"
OWNER = "bot:plan-lite-owner"
STRANGER = "bot:plan-lite-stranger"

#: Ключи, которые plan_lite вправе нести (В-5): факты и форма обязательства.
PLAN_LITE_KEYS = {"plan_id", "goal_key", "actions"}
ACTION_KEYS = {"action_type", "cadence", "target_count", "done_count", "bucket"}
#: Слова результата, которых в документе быть не может ни в одном ключе.
FORBIDDEN_KEY_WORDS = ("percent", "progress", "achiev", "score", "weight", "result")


@pytest.fixture(autouse=True)
def _token_and_flag(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.PLAN_LITE_ENABLED = True


@pytest.fixture
def owner(db) -> User:
    return User.objects.create_user(
        username=OWNER, password="x", role="client", phone="+79995002101", is_proxy=True,
    )


@pytest.fixture
def stranger(db) -> User:
    return User.objects.create_user(
        username=STRANGER, password="x", role="client", phone="+79995002102", is_proxy=True,
    )


@pytest.fixture
def goal(owner) -> ClientGoal:
    return ClientGoal.objects.create(client=owner, goal_key="tone_up", source_channel="miniapp")


def _api(external_user_id: str = OWNER) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _post(client: APIClient, goal: ClientGoal, actions: list[dict]):
    return client.post(PLAN_URL, {"goal_id": str(goal.id), "actions": actions}, format="json")


FOOD_3_PER_WEEK = {"action_type": "log_food", "cadence": "per_week", "target_count": 3}
WATER_2_PER_DAY = {"action_type": "log_water", "cadence": "per_day", "target_count": 2}
BOOK_1_PER_WEEK = {"action_type": "book_service", "cadence": "per_week", "target_count": 1}


# ─── сторожа границы 1 ───────────────────────────────────────────────────────

FORBIDDEN_IMPORT_NAMES = {
    "body_observation_gate",
    "record_observation",
    "ProgressObservation",
    "NutritionProfile",
    "weight_kg",
}


def _plan_lite_modules() -> list[Path]:
    root = Path(__file__).resolve().parents[1]
    return sorted(root.glob("plan_lite*.py"))


def _imported_names(source: str) -> set[str]:
    names: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
            if node.module:
                names.update(node.module.split("."))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.update(alias.name.split("."))
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


class TestNoBodyObservationInPlanLite:
    def test_plan_lite_modules_do_not_import_body_observation_names(self) -> None:
        modules = _plan_lite_modules()
        # Присутствие раньше отсутствия: пустая перепись читалась бы как
        # «никто ничего не импортирует».
        assert len(modules) >= 2, [m.name for m in modules]
        imported = {m.name: _imported_names(m.read_text(encoding="utf-8")) for m in modules}
        assert any("PersonalPlan" in names for names in imported.values()), imported
        for name, names in imported.items():
            hit = sorted(FORBIDDEN_IMPORT_NAMES & names)
            assert not hit, f"{name} трогает наблюдения тела: {hit}"

    def test_the_import_census_catches_a_planted_import(self) -> None:
        """Ложный вход: сканер обязан покраснеть от одной подсунутой строки."""
        planted = "from wellness.services import body_observation_gate\n"
        assert FORBIDDEN_IMPORT_NAMES & _imported_names(planted)
        planted_attr = "x = profile.weight_kg\n"
        assert FORBIDDEN_IMPORT_NAMES & _imported_names(planted_attr)

    def test_creating_a_plan_writes_no_progress_observation(self, owner, goal) -> None:
        resp = _post(_api(), goal, [FOOD_3_PER_WEEK, BOOK_1_PER_WEEK])

        assert resp.status_code == 201, resp.content[:400]
        assert PersonalPlan.objects.filter(user=owner).count() == 1  # присутствие
        assert ProgressObservation.objects.filter(user=owner).count() == 0


# ─── писатель ────────────────────────────────────────────────────────────────


class TestWriter:
    def test_post_creates_plan_with_goal_and_actions(self, owner, goal) -> None:
        resp = _post(_api(), goal, [FOOD_3_PER_WEEK, WATER_2_PER_DAY])

        assert resp.status_code == 201, resp.content[:400]
        plan = PersonalPlan.objects.get(user=owner, status=PersonalPlan.Status.ACTIVE)
        assert plan.goal_id == goal.id
        assert plan.goal_key == "tone_up"
        actions = list(plan.actions.order_by("created_at"))
        assert [(a.action_type, a.cadence, a.target_count) for a in actions] == [
            ("log_food", "per_week", 3),
            ("log_water", "per_day", 2),
        ]
        body = resp.json()["data"]
        assert body["plan_id"] == str(plan.id)
        assert body["goal_key"] == "tone_up"

    def test_second_active_plan_is_409(self, owner, goal) -> None:
        assert _post(_api(), goal, [FOOD_3_PER_WEEK]).status_code == 201

        resp = _post(_api(), goal, [WATER_2_PER_DAY])

        assert resp.status_code == 409, resp.content[:400]
        assert resp.json()["error"]["code"] == "PLAN_LITE_ALREADY_ACTIVE"
        assert PersonalPlan.objects.filter(user=owner).count() == 1

    def test_book_service_is_an_accepted_action_type(self, owner, goal) -> None:
        resp = _post(_api(), goal, [BOOK_1_PER_WEEK])

        assert resp.status_code == 201, resp.content[:400]
        assert PlanAction.objects.get(plan__user=owner).action_type == PlanAction.ActionType.BOOK_SERVICE

    @pytest.mark.parametrize(
        "actions",
        [
            [],
            [FOOD_3_PER_WEEK, WATER_2_PER_DAY, BOOK_1_PER_WEEK, FOOD_3_PER_WEEK],
            [{"action_type": "weigh_in", "cadence": "per_day", "target_count": 1}],
            [{"action_type": "log_food", "cadence": "per_week", "target_count": 0}],
        ],
        ids=["none", "four", "unknown-type", "zero-target"],
    )
    def test_actions_are_1_to_3_curated_and_positive(self, owner, goal, actions) -> None:
        resp = _post(_api(), goal, actions)

        assert resp.status_code == 400, resp.content[:400]
        assert PersonalPlan.objects.filter(user=owner).count() == 0

    def test_goal_must_be_the_callers_active_goal(self, owner, stranger, goal) -> None:
        """Чужая цель — «не найдено», не «чужое»: по коду нельзя перебирать."""
        resp = _post(_api(STRANGER), goal, [FOOD_3_PER_WEEK])
        assert resp.status_code == 404, resp.content[:400]
        assert resp.json()["error"]["code"] == "NOT_FOUND"

        goal.state = ClientGoal.State.ARCHIVED
        goal.save(update_fields=["state"])
        resp = _post(_api(), goal, [FOOD_3_PER_WEEK])
        assert resp.status_code == 404, resp.content[:400]
        assert resp.json()["error"]["code"] == "NOT_FOUND"
        assert PersonalPlan.objects.count() == 0

    def test_delete_closes_append_only(self, owner, goal) -> None:
        plan_id = _post(_api(), goal, [FOOD_3_PER_WEEK]).json()["data"]["plan_id"]

        resp = _api().delete(PLAN_URL)

        assert resp.status_code == 200, resp.content[:400]
        plan = PersonalPlan.objects.get(pk=plan_id)  # строка осталась
        assert plan.status == PersonalPlan.Status.CLOSED_BY_USER
        assert plan.closed_at is not None
        assert plan.actions.count() == 1  # история обязательств тоже
        # Закрытый план не мешает новому — и второй DELETE уже «нет активного».
        assert _api().delete(PLAN_URL).status_code == 404
        assert _post(_api(), goal, [WATER_2_PER_DAY]).status_code == 201


# ─── чтение: plan_lite в wellness-context ────────────────────────────────────


def _ctx(client: APIClient) -> dict:
    resp = client.get(CTX_URL)
    assert resp.status_code == 200, resp.content[:400]
    return resp.json()["data"]


class TestRead:
    def test_wellness_context_carries_plan_lite_with_done_count_by_bucket(
        self, owner, goal,
    ) -> None:
        """per_week log_food — ДНИ с записью (две записи в один день — один
        день), per_day log_water — записи за сегодня, не больше target."""
        _post(_api(), goal, [FOOD_3_PER_WEEK, WATER_2_PER_DAY])
        now = timezone.now()
        week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        day1 = week_start + timedelta(hours=9)
        for at in (day1, day1 + timedelta(hours=3)):  # один день, две записи
            FoodLog.objects.create(
                user=owner, dish_name="борщ", portion_multiplier=1.0, calories=100.0,
                meal_type="lunch", entry_origin=FoodLog.EntryOrigin.TEXT_ESTIMATED_CONFIRMED,
                logged_at=at,
            )
        for _ in range(3):  # сегодня три стакана при target 2
            WaterEntry.objects.create(user=owner, ts=now, ml=250, water_ml=250.0)

        doc = _ctx(_api())

        assert doc["plan_lite"]["goal_key"] == "tone_up"
        by_type = {a["action_type"]: a for a in doc["plan_lite"]["actions"]}
        assert by_type["log_food"]["done_count"] == 1
        assert by_type["log_food"]["target_count"] == 3
        assert by_type["log_water"]["done_count"] == 2
        assert by_type["log_water"]["target_count"] == 2
        assert by_type["log_food"]["bucket"]["start"] == week_start.date().isoformat()

    def test_plan_lite_is_present_even_though_gates_d_and_o_are_closed(self, owner, goal) -> None:
        """Гейт O к плану без наблюдений не относится; документ остаётся
        gated по своим проекциям, а plan_lite — рядом, не внутри."""
        _post(_api(), goal, [FOOD_3_PER_WEEK])

        doc = _ctx(_api())

        assert doc["gated"] is not None  # гейты закрыты, как и были
        assert doc["plan"] is None and doc["outcomes"] == []
        assert doc["plan_lite"]["actions"][0]["action_type"] == "log_food"

    def test_plan_lite_has_no_result_fields(self, owner, goal) -> None:
        """В-5: факт действия ≠ результат — ни процента, ни шкалы, ни итога."""
        _post(_api(), goal, [FOOD_3_PER_WEEK, BOOK_1_PER_WEEK])

        plan_lite = _ctx(_api())["plan_lite"]

        assert set(plan_lite) == PLAN_LITE_KEYS
        assert plan_lite["actions"], "пустой список — сторож ничего не проверил"
        for action in plan_lite["actions"]:
            assert set(action) == ACTION_KEYS
        flat = " ".join(set(plan_lite) | {k for a in plan_lite["actions"] for k in a}).lower()
        assert not any(word in flat for word in FORBIDDEN_KEY_WORDS), flat

    def test_no_active_plan_reads_as_null(self, owner) -> None:
        assert _ctx(_api())["plan_lite"] is None


# ─── флаг ────────────────────────────────────────────────────────────────────


class TestFlag:
    def test_flag_off_post_is_404_plan_lite_disabled_not_5xx(self, owner, goal, settings) -> None:
        settings.PLAN_LITE_ENABLED = False

        resp = _post(_api(), goal, [FOOD_3_PER_WEEK])

        assert resp.status_code == 404, resp.content[:400]
        assert resp.json()["error"]["code"] == "PLAN_LITE_DISABLED"
        assert PersonalPlan.objects.count() == 0
        assert _api().delete(PLAN_URL).status_code == 404

    def test_flag_off_read_gives_plan_lite_null(self, owner, goal, settings) -> None:
        _post(_api(), goal, [FOOD_3_PER_WEEK])  # план есть
        settings.PLAN_LITE_ENABLED = False

        doc = _ctx(_api())

        assert "plan_lite" in doc
        assert doc["plan_lite"] is None


# ─── adherence.compute_plan_adherence на плане без PlanOutcomeLink ───────────


class TestLegacyAdherenceOnPlanLite:
    def test_compute_plan_adherence_survives_a_plan_lite_plan(self, owner, goal) -> None:
        """У Plan Lite нет PlanOutcomeLink и есть book_service — старая
        производная DRF-1334 не падает ни на том, ни на другом."""
        from wellness.adherence import compute_plan_adherence

        _post(_api(), goal, [FOOD_3_PER_WEEK, BOOK_1_PER_WEEK])
        plan = PersonalPlan.objects.get(user=owner)
        today = timezone.localdate()

        rows = compute_plan_adherence(plan, today - timedelta(days=7), today)

        assert {r.action_type for r in rows} == {"log_food", "book_service"}
        assert all(r.fulfilled_count == 0 for r in rows)


# ─── PR-1b: goal_id необязателен — берётся активная цель вызывающего ─────────


class TestGoalIdOptional:
    def test_post_without_goal_id_uses_the_callers_active_goal(self, owner, goal) -> None:
        resp = _api().post(PLAN_URL, {"actions": [FOOD_3_PER_WEEK]}, format="json")

        assert resp.status_code == 201, resp.content[:400]
        plan = PersonalPlan.objects.get(user=owner)
        assert plan.goal_id == goal.id and plan.goal_key == "tone_up"
        assert resp.json()["data"]["goal_key"] == "tone_up"

    def test_post_without_goal_id_and_without_an_active_goal_is_404(self, owner) -> None:
        resp = _api().post(PLAN_URL, {"actions": [FOOD_3_PER_WEEK]}, format="json")

        assert resp.status_code == 404, resp.content[:400]
        assert resp.json()["error"]["code"] == "NOT_FOUND"
        assert PersonalPlan.objects.count() == 0

    def test_post_with_a_malformed_goal_id_is_still_400(self, owner, goal) -> None:
        resp = _api().post(PLAN_URL, {"goal_id": "not-a-uuid", "actions": [FOOD_3_PER_WEEK]}, format="json")

        assert resp.status_code == 400
        assert PersonalPlan.objects.count() == 0
