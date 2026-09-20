"""C03 в живой путь — документ решения по макету DRF-1178 (DRF-2177, К-2 эпика DRF-2175).

Решение владельца §60 (20.09): «сделать надо так как в документах и макетах».
Замер 20.09 (`03_client_journey_mockups_vs_reality.md`): у человека с целью
документ вопросов не несёт — их кладут только при открытом проходе или без
цели; вместо вопросов — семь чипов и «Найти услугу» всегда. На живом снимке
владельца ровно это.

Что заперто здесь:

1. цель есть, прохода нет, сужающие шаги под эту цель не отвечены → первый
   сужающий шаг сразу (макет C03.2 «первый вопрос — показываем сразу»),
   шаг цели считается отвеченным самой целью (`progress` 2 из 3);
2. ответ на такой шаг создаёт проход, привязанный к активной цели; последний
   ответ закрывает проход;
3. всё отвечено → ``missing == []`` и ``next = return_to_chat`` (C03.5:
   «Спасибо! Этого достаточно…» → в чат); повторный GET вопросов не
   возвращает — контекст собран;
4. семь целей (``suggestions``) при активной цели скрыты — только за
   «Изменить» (``start_anketa`` → шаг цели с опциями);
5. ``browse_catalog`` («Найти услугу») в документе больше нет, пока анкета
   включена (§60: «Найти услугу» с экрана уходит);
6. C-2 по сути сохранён: названная услуга — контекст собран без вопросов,
   ответов ноль; нераспознанный текст — уточнение прежде вопросов;
7. «Не знаю» — на КАЖДОМ сужающем шаге (макет: «всегда доступно»);
8. подпись свободного ввода — «Рассказать своими словами» (макет).

Чат → вопросы (E2E DRF-1174 «не хватает контекста → C03») — целевой
UX-контракт, не runtime (ruling §61); входы — H01 и меню. Здесь не строится.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals import anketa
from goals.decision_context import (
    INTENT_FORMULATE_OWN,
    INTENT_START_ANKETA,
    NEXT_BROWSE_CATALOG,
    NEXT_RETURN_TO_CHAT,
    build_decision_context,
)
from goals.models import ClientGoal, GoalAnketaAnswer, GoalAnketaRun
from services.models import GoalOption, ServiceCategory
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-2177"
SELECT_URL = "/api/v1/internal/me/goals/select/"
CTX_URL = "/api/v1/internal/me/decision-context/"
EXTERNAL_USER_ID = "bot:c03-live"

AREA, FEELING = anketa.ANKETA_STEPS[0], anketa.ANKETA_STEPS[1]


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995002177", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.GOAL_ANKETA_ENABLED = True


@pytest.fixture
def goal_options(db):
    return [
        GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=10),
        GoalOption.objects.create(key="self_care", label="Привести себя в порядок", sort_order=20),
    ]


@pytest.fixture
def catalog(db):
    return ServiceCategory.objects.create(name="Маникюр", slug="manicure")


def _api():
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


def _answer(api, step: str, **kwargs):
    return api.post(
        SELECT_URL,
        {"answer": {"step": step, **kwargs}, "source_channel": "miniapp"},
        format="json",
    )


def _pick_goal(api, key: str = "self_care"):
    """Прямой выбор цели чипом — как на снимке владельца 20.09."""
    resp = api.post(SELECT_URL, {"goal_key": key, "source_channel": "miniapp"}, format="json")
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


def _first_option(step: anketa.AnketaStep) -> dict:
    key = step.options[0][0]
    return {"option_keys": [key]} if step.mode == anketa.MODE_MULTI else {"option_key": key}


@pytest.mark.django_db
class TestFirstQuestionComesFromTheGoal:
    def test_goal_without_answers_gets_the_first_narrowing_step_at_once(
        self, customer, token, goal_options
    ):
        api = _api()
        doc = _pick_goal(api)

        # Цель есть — и СРАЗУ первый сужающий вопрос, без вступления.
        assert doc["known"]["goal"]["goal_key"] == "self_care"
        first = doc["missing"][0]
        assert first["kind"] == anketa.MISSING_GOAL_ANKETA
        assert first["step"] == AREA.key
        # Формулировка — под цель (prompt_by_goal), как в проходе.
        assert first["prompt"] == anketa.shown_prompt(AREA, "self_care")
        # Шаг цели отвечен самой целью: это 2 из 3, и не последний.
        assert first["progress"] == {"index": 2, "total": anketa.TOTAL_STEPS, "is_last": False}
        # Проход GET не создаёт — как и прежде.
        assert not GoalAnketaRun.objects.filter(client=customer, completed_at__isnull=True).exists()

    def test_goal_carries_its_own_label_and_change_is_offered(self, customer, token, goal_options):
        """Семь целей при цели скрывает экран (§60); документ даёт ему
        подпись цели рядом с целью, а не через ряд ``suggestions``."""
        api = _api()
        doc = _pick_goal(api)
        assert doc["known"]["goal"]["label"] == "Привести себя в порядок"
        # «Изменить» = start_anketa — предложен; ряд целей вернётся шагом цели.
        assert INTENT_START_ANKETA in [i["id"] for i in doc["intents"]]

    def test_free_text_goal_label_is_the_persons_words(self, customer, token, catalog):
        api = _api()
        doc = api.post(
            SELECT_URL, {"goal_text": "хочу маникюр", "source_channel": "miniapp"}, format="json"
        ).json()["data"]
        assert doc["known"]["goal"]["label"] == "хочу маникюр"

    def test_find_service_is_gone_while_questions_remain(self, customer, token, goal_options):
        doc = _pick_goal(_api())
        assert doc["missing"], "предусловие: вопрос на экране есть"
        assert doc["next"] is None

    def test_no_goal_first_screen_has_no_find_service_either(self, customer, token, goal_options):
        doc = _api().get(CTX_URL).json()["data"]
        assert doc["missing"][0]["step"] == anketa.GOAL_STEP_KEY
        assert doc["next"] is None


@pytest.mark.django_db
class TestAnswersBindToTheGoalAndFinish:
    def test_answer_creates_a_run_bound_to_the_active_goal(self, customer, token, goal_options):
        api = _api()
        _pick_goal(api)

        resp = _answer(api, AREA.key, **_first_option(AREA))
        assert resp.status_code == 200, resp.content
        doc = resp.json()["data"]

        run = GoalAnketaRun.objects.get(client=customer, completed_at__isnull=True)
        assert run.goal is not None and run.goal.goal_key == "self_care"
        # Следующий вопрос — второй сужающий; «Уже учла» несёт первый ответ.
        assert doc["missing"][0]["step"] == FEELING.key
        assert [a["step"] for a in doc["known"]["anketa"]] == [AREA.key]
        # DRF-2173: за feeling — необязательный срок; последний теперь он.
        assert doc["missing"][0]["progress"]["is_last"] is False
        assert anketa.ANKETA_STEPS[-1].key == "deadline"

    def test_last_answer_closes_the_run_and_sends_the_person_back_to_chat(
        self, customer, token, goal_options
    ):
        api = _api()
        _pick_goal(api)
        assert _answer(api, AREA.key, **_first_option(AREA)).status_code == 200
        assert _answer(api, FEELING.key, **_first_option(FEELING)).status_code == 200
        # DRF-2173: проход закрывает необязательный шаг срока («без срока»).
        resp = _answer(api, "deadline", option_key="no_deadline")
        assert resp.status_code == 200, resp.content
        doc = resp.json()["data"]

        run = GoalAnketaRun.objects.get(client=customer)
        assert run.completed_at is not None
        assert run.goal is not None and run.goal.goal_key == "self_care"
        assert set(GoalAnketaAnswer.objects.filter(run=run).values_list("step_key", flat=True)) == {
            AREA.key, FEELING.key, "deadline"
        }
        assert doc["missing"] == []
        assert doc["next"]["id"] == NEXT_RETURN_TO_CHAT
        assert doc["known"]["goal"]["goal_key"] == "self_care"

    def test_collected_context_is_not_asked_again(self, customer, token, goal_options):
        api = _api()
        _pick_goal(api)
        _answer(api, AREA.key, **_first_option(AREA))
        _answer(api, FEELING.key, **_first_option(FEELING))
        assert _answer(api, "deadline", option_key="no_deadline").status_code == 200

        doc = api.get(CTX_URL).json()["data"]
        assert doc["next"]["id"] == NEXT_RETURN_TO_CHAT
        assert doc["missing"] == []

    def test_stale_narrowing_answer_after_collected_context_is_refused(
        self, customer, token, goal_options
    ):
        """C-1 держится: контекст собран → протухший ответ на сужающий шаг —
        409, нового прохода нет (по ревью: условие API = условию документа)."""
        api = _api()
        _pick_goal(api)
        _answer(api, AREA.key, **_first_option(AREA))
        _answer(api, FEELING.key, **_first_option(FEELING))
        assert _answer(api, "deadline", option_key="no_deadline").status_code == 200

        resp = _answer(api, AREA.key, **_first_option(AREA))
        assert resp.status_code == 409, resp.content
        assert GoalAnketaRun.objects.filter(client=customer).count() == 1
        assert not GoalAnketaRun.objects.filter(client=customer, completed_at__isnull=True).exists()

    def test_unresolved_text_goal_does_not_accept_a_narrowing_answer(
        self, customer, token, goal_options
    ):
        """Уточнение прежде вопросов — и в API: пока цель не разрешена,
        ответ на сужающий шаг не привязывает проход к неясной цели."""
        api = _api()
        api.post(
            SELECT_URL, {"goal_text": "хочу что-то для рук", "source_channel": "miniapp"}, format="json"
        )
        resp = _answer(api, AREA.key, **_first_option(AREA))
        assert resp.status_code == 409, resp.content
        assert GoalAnketaRun.objects.count() == 0

    def test_chip_mid_run_rebinds_and_asks_the_new_goal_to_the_end(
        self, customer, token, goal_options
    ):
        """Сторож от вечных вопросов: чип посреди прохода закрывает его новой
        целью; под неё — вопросы (первый подтверждением), проход завершается."""
        api = _api()
        _pick_goal(api, "self_care")
        _answer(api, AREA.key, **_first_option(AREA))
        doc = _pick_goal(api, "relax")
        assert doc["missing"][0]["step"] == AREA.key
        assert doc["missing"][0]["mode"] == "confirm"
        assert _answer(api, AREA.key, confirm=True).status_code == 200
        assert _answer(api, FEELING.key, **_first_option(FEELING)).status_code == 200
        doc = _answer(api, "deadline", option_key="no_deadline").json()["data"]
        assert doc["next"]["id"] == NEXT_RETURN_TO_CHAT
        assert doc["missing"] == []

    def test_a_new_goal_asks_its_own_questions(self, customer, token, goal_options):
        """Контекст собран ПОД ЦЕЛЬ, не под человека: сменил цель — вопросы снова."""
        api = _api()
        _pick_goal(api, "self_care")
        _answer(api, AREA.key, **_first_option(AREA))
        _answer(api, FEELING.key, **_first_option(FEELING))
        assert _answer(api, "deadline", option_key="no_deadline").status_code == 200

        doc = _pick_goal(api, "relax")
        assert doc["missing"][0]["step"] == AREA.key
        # Прошлый ответ известен — подтверждение, не переспрос (DRF-1745).
        assert doc["missing"][0]["mode"] == "confirm"
        assert doc["missing"][0]["known_value"]["option_key"] == AREA.options[0][0]


@pytest.mark.django_db
class TestC2StaysInSubstance:
    """Названная услуга — без вопросов; нераспознанный текст — уточнение прежде вопросов."""

    def test_named_service_needs_no_questions_and_no_answers(self, customer, token, catalog):
        api = _api()
        doc = api.post(
            SELECT_URL, {"goal_text": "хочу маникюр", "source_channel": "miniapp"}, format="json"
        ).json()["data"]
        assert doc["known"]["goal"]["goal_text"] == "хочу маникюр"
        assert doc["next"]["id"] == NEXT_RETURN_TO_CHAT
        assert doc["missing"] == []
        assert GoalAnketaAnswer.objects.count() == 0
        assert api.get(CTX_URL).json()["data"]["missing"] == []

    def test_unrecognised_text_is_clarified_before_any_narrowing_step(
        self, customer, token, goal_options
    ):
        api = _api()
        doc = api.post(
            SELECT_URL, {"goal_text": "хочу что-то для рук", "source_channel": "miniapp"}, format="json"
        ).json()["data"]
        assert [m["kind"] for m in doc["missing"]] == ["goal_clarification"]
        assert doc["next"] is None


@pytest.mark.django_db
class TestMockTexts:
    def test_dont_know_is_available_on_every_narrowing_step(self):
        for step in anketa.ANKETA_STEPS:
            assert step.escape is True, step.key
            item = anketa.as_missing_item(step, answered_keys={anketa.GOAL_STEP_KEY})
            assert item["options"][-1]["role"] == anketa.OPTION_ROLE_ESCAPE

    def test_free_text_intent_reads_as_the_mock(self, customer, token, goal_options):
        doc = build_decision_context(customer)
        label = next(i["label"] for i in doc["intents"] if i["id"] == INTENT_FORMULATE_OWN)
        assert label == "Рассказать своими словами"


@pytest.mark.django_db
class TestFlagOffIsUntouched:
    def test_anketa_off_returns_the_drf1190_document(self, customer, goal_options, settings):
        settings.GOAL_ANKETA_ENABLED = False
        ClientGoal.objects.create(client=customer, goal_key="relax", source_channel="miniapp")
        doc = build_decision_context(customer)
        assert doc["missing"] == []
        assert [s["key"] for s in doc["suggestions"]] == ["relax", "self_care"]
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG
