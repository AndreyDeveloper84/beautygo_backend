"""Анкета цели — серверная последовательность вопросов (DRF-1451).

Что здесь заперто:

- анкета доходит до конца и по завершении цель СФОРМИРОВАНА
  (``known.goal`` заполнен, ``missing`` пуст);
- последовательность серверная: пропустить шаг из клиента нельзя;
- **анкета не ворота** — путь «назвал услугу → попал к подбору»
  проходится, НЕ ответив ни на один вопрос (условие C-2 поправки A-1
  к BOT-001, §24). Это отдельный класс ``TestAnketaIsNotAGate``;
- прежние три пути DRF-1190 живы и не отменены;
- повторный проход возможен сколько угодно раз (DRF-1225 / C-4);
- выключенный флаг возвращает ровно прежний документ.
"""
from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from goals import anketa
from goals.decision_context import (
    INTENT_START_ANKETA,
    MISSING_GOAL,
    MISSING_GOAL_CLARIFICATION,
    NEXT_BROWSE_CATALOG,
    build_decision_context,
)
from goals.models import ClientGoal, GoalAnketaAnswer, GoalAnketaRun
from services.models import GoalOption, SalonService, ServiceCategory
from tenants.models import Tenant
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-anketa"
CTX_URL = "/api/v1/internal/me/decision-context/"
SELECT_URL = "/api/v1/internal/me/goals/select/"
EXTERNAL_USER_ID = "bot:anketa"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79995000042", is_proxy=True,
    )


@pytest.fixture
def token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN
    settings.GOAL_ANKETA_ENABLED = True


@pytest.fixture
def goal_options(db):
    return [
        GoalOption.objects.create(key="relax", label="Расслабиться", sort_order=10),
        GoalOption.objects.create(key="glow", label="Сиять", sort_order=20),
    ]


@pytest.fixture
def catalog(db):
    """Каталог, в котором есть «Маникюр» — та самая названная услуга."""
    category = ServiceCategory.objects.create(name="Маникюр", slug="manicure")
    return category


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


def _kinds(doc) -> list[str]:
    return [m["kind"] for m in doc["missing"]]


# ---------------------------------------------------------------------------
# Форма документа
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestDocumentShape:
    def test_first_entry_asks_the_first_anketa_step(self, customer, settings):
        settings.GOAL_ANKETA_ENABLED = True
        doc = build_decision_context(customer)

        assert doc["version"] == 2
        assert doc["known"]["goal"] is None
        assert _kinds(doc) == [anketa.MISSING_GOAL_ANKETA]

        item = doc["missing"][0]
        assert item["step"] == anketa.ANKETA_STEPS[0].key
        assert item["progress"] == {"index": 1, "total": anketa.TOTAL_STEPS}
        assert [o["key"] for o in item["options"]] == [
            key for key, _ in anketa.ANKETA_STEPS[0].options
        ]

    def test_step_item_carries_only_renderable_fields(self, customer, settings):
        """Инвариант Ответа 3, распространённый на шаг анкеты.

        Ни одного поля, из которого экран мог бы вычислить ДРУГОЕ
        содержимое: нет ни списка оставшихся шагов, ни признака
        «последний», ни следующего вопроса. Есть ровно то, что рисуется.
        """
        settings.GOAL_ANKETA_ENABLED = True
        item = build_decision_context(customer)["missing"][0]
        assert set(item) == {
            "kind", "prompt", "step", "options", "allow_free_text", "progress",
        }
        assert set(item["progress"]) == {"index", "total"}
        for option in item["options"]:
            assert set(option) == {"key", "label"}

    def test_old_kinds_survive_the_new_shape(self, customer, settings):
        """Потребитель, читающий только prompt, не сломан.

        ``GoalInviteCard`` в мини-аппе рисует ``item.prompt`` и больше
        ничего. Форма шага обязана оставаться для него читаемой.
        """
        settings.GOAL_ANKETA_ENABLED = True
        item = build_decision_context(customer)["missing"][0]
        assert item["kind"] and isinstance(item["prompt"], str) and item["prompt"]

    def test_flag_off_returns_the_drf1190_document(self, customer, settings):
        settings.GOAL_ANKETA_ENABLED = False
        doc = build_decision_context(customer)
        assert _kinds(doc) == [MISSING_GOAL]
        assert set(doc["missing"][0]) == {"kind", "prompt"}
        assert [i["id"] for i in doc["intents"]] == [
            "choose_suggested", "formulate_own", "need_guidance",
        ]


# ---------------------------------------------------------------------------
# Проход анкеты до цели
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAnketaFormsAGoal:
    def test_full_pass_ends_with_a_goal_and_nothing_left_to_ask(
        self, customer, token, goal_options,
    ):
        api = _api()

        doc = api.get(CTX_URL).json()["data"]
        assert doc["known"]["goal"] is None

        for step in anketa.ANKETA_STEPS:
            item = doc["missing"][0]
            assert item["step"] == step.key
            resp = _answer(api, step.key, option_key=step.options[0][0])
            assert resp.status_code == 200, resp.content
            doc = resp.json()["data"]

        # Финальный шаг — сама цель: варианты пришли из GoalOption.
        final = doc["missing"][0]
        assert final["step"] == anketa.FINAL_STEP_KEY
        assert final["allow_free_text"] is True
        assert [o["key"] for o in final["options"]] == ["relax", "glow"]

        doc = _answer(api, anketa.FINAL_STEP_KEY, option_key="relax").json()["data"]

        assert doc["known"]["goal"]["goal_key"] == "relax"
        assert doc["missing"] == []
        assert doc["next"] == {"id": NEXT_BROWSE_CATALOG, "label": "Найти услугу"}

        run = GoalAnketaRun.objects.get(client=customer)
        assert run.completed_at is not None
        assert run.goal is not None and run.goal.goal_key == "relax"
        # Ответы на сужающие шаги сохранены — корпус OD-2.
        assert set(
            GoalAnketaAnswer.objects.filter(run=run).values_list("step_key", flat=True)
        ) == {step.key for step in anketa.ANKETA_STEPS} | {anketa.FINAL_STEP_KEY}

    def test_step_echo_must_match_the_expected_step(self, customer, token):
        """Пропустить вопрос из клиента нельзя — последовательность серверная."""
        api = _api()
        resp = _answer(
            api, anketa.ANKETA_STEPS[1].key,
            option_key=anketa.ANKETA_STEPS[1].options[0][0],
        )
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "ANKETA_STEP_MISMATCH"
        assert resp.json()["error"]["details"]["expected_step"] == (
            anketa.ANKETA_STEPS[0].key
        )
        assert GoalAnketaAnswer.objects.count() == 0
        # Отклонённый ответ не должен оставлять после себя проход.
        # Раньше оставлял — и человек с целью получал анкету при каждом
        # открытии приложения, навсегда.
        assert GoalAnketaRun.objects.count() == 0

    def test_unknown_option_is_rejected(self, customer, token):
        api = _api()
        resp = _answer(api, anketa.ANKETA_STEPS[0].key, option_key="not-an-option")
        assert resp.status_code == 400
        assert GoalAnketaAnswer.objects.count() == 0
        assert GoalAnketaRun.objects.count() == 0

    def test_free_text_is_refused_on_a_closed_step(self, customer, token):
        api = _api()
        resp = _answer(api, anketa.ANKETA_STEPS[0].key, text="что-нибудь своё")
        assert resp.status_code == 400
        assert GoalAnketaAnswer.objects.count() == 0
        assert GoalAnketaRun.objects.count() == 0

    def test_stale_answer_from_a_client_with_a_goal_leaves_no_open_run(
        self, customer, token, goal_options,
    ):
        """Самый дорогой случай отказа, и он же самый обыденный.

        Документ на экране протух (человек выбрал цель из бота, пока
        мини-апп был открыт; или запрос повторён после таймаута). Ответ
        приходит на шаг, которого сервер не ждёт.

        Если такой отказ оставит открытый проход, ``build_decision_context``
        будет возвращать вопрос анкеты на КАЖДОМ запросе — и человек с
        целью станет получать анкету при каждом открытии приложения.
        Ровно то, что решение владельца запрещает.
        """
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="bot",
        )
        api = _api()
        resp = _answer(api, anketa.FINAL_STEP_KEY, option_key="relax")
        assert resp.status_code == 409

        assert GoalAnketaRun.objects.count() == 0
        # И следующее открытие приложения вопросов не показывает.
        assert build_decision_context(customer)["missing"] == []

    def test_step_mismatch_uses_the_registered_error_code(self, customer, token):
        api = _api()
        resp = _answer(
            api, anketa.ANKETA_STEPS[1].key,
            option_key=anketa.ANKETA_STEPS[1].options[0][0],
        )
        assert resp.json()["error"]["code"] == "ANKETA_STEP_MISMATCH"

    def test_option_key_is_checked_even_when_the_step_has_no_options(
        self, customer, token,
    ):
        """Салон без активных GoalOption: список финального шага пуст.

        Прежняя проверка (`if option_key and expected.options`) на пустом
        списке замолкала, и ЛЮБОЙ слаг уезжал прямо в
        ``ClientGoal.goal_key`` — оттуда в события воронки и в резолвер,
        где молча не находил ничего.
        """
        api = _api()
        for step in anketa.ANKETA_STEPS:
            _answer(api, step.key, option_key=step.options[0][0])
        assert not GoalOption.objects.filter(is_active=True).exists()

        # Латиницей: SlugField кириллицу отвергает сам, и тест прошёл бы
        # по чужой причине, ничего не проверив.
        resp = _answer(api, anketa.FINAL_STEP_KEY, option_key="anything-at-all")
        assert resp.status_code == 400
        assert ClientGoal.objects.count() == 0

    def test_final_step_accepts_free_text_and_forms_the_goal(
        self, customer, token, goal_options, catalog,
    ):
        api = _api()
        for step in anketa.ANKETA_STEPS:
            _answer(api, step.key, option_key=step.options[0][0])
        doc = _answer(api, anketa.FINAL_STEP_KEY, text="Маникюр").json()["data"]

        goal = ClientGoal.objects.get(client=customer, is_active=True)
        assert goal.goal_key is None
        assert goal.goal_text == "Маникюр"  # дословно, OD-2
        assert doc["missing"] == []  # названа услуга — уточнять нечего

    def test_get_does_not_start_a_run(self, customer, token):
        """GET не пишет: первый вопрос виден, строки прохода ещё нет."""
        api = _api()
        api.get(CTX_URL)
        api.get(CTX_URL)
        assert GoalAnketaRun.objects.count() == 0


# ---------------------------------------------------------------------------
# АНКЕТА НЕ ВОРОТА — условие C-2 поправки A-1
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAnketaIsNotAGate:
    """«Назвал услугу → попал к подбору», НЕ ответив ни на один вопрос.

    Владелец уточнил это прямым текстом 03.09.2026. Здесь оно и заперто:
    во всех тестах класса ``GoalAnketaAnswer.objects.count() == 0``.
    """

    def test_named_service_reaches_the_catalog_with_zero_answers(
        self, customer, token, catalog,
    ):
        api = _api()

        # Человек открыл приложение и видит первый вопрос анкеты…
        doc = api.get(CTX_URL).json()["data"]
        assert doc["missing"][0]["kind"] == anketa.MISSING_GOAL_ANKETA

        # …но он знает, чего хочет, и пишет это на той же поверхности.
        doc = api.post(
            SELECT_URL,
            {"goal_text": "хочу маникюр", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]

        # Цель готова, спрашивать нечего, сервер называет следующий шаг.
        assert doc["known"]["goal"]["goal_text"] == "хочу маникюр"
        assert doc["missing"] == []
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG

        # Ни одного ответа на вопрос анкеты — это и есть суть теста.
        assert GoalAnketaAnswer.objects.count() == 0

        # И следующий GET не роняет обратно в вопросы.
        assert api.get(CTX_URL).json()["data"]["missing"] == []

    def test_suggestion_chip_reaches_the_catalog_with_zero_answers(
        self, customer, token, goal_options,
    ):
        api = _api()
        api.get(CTX_URL)
        doc = api.post(
            SELECT_URL,
            {"goal_key": "relax", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]

        assert doc["known"]["goal"]["goal_key"] == "relax"
        assert doc["missing"] == []
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG
        assert GoalAnketaAnswer.objects.count() == 0

    def test_leaving_mid_anketa_closes_the_run_instead_of_dragging_back(
        self, customer, token, catalog,
    ):
        """Назвал услугу на втором вопросе — и не вернулся в вопросы.

        Оставить проход открытым значило бы на следующем же запросе
        снова показать вопрос: анкета стала бы воротами с отсрочкой.
        """
        api = _api()
        first = anketa.ANKETA_STEPS[0]
        _answer(api, first.key, option_key=first.options[0][0])
        assert build_decision_context(customer)["missing"][0]["step"] == (
            anketa.ANKETA_STEPS[1].key
        )

        api.post(
            SELECT_URL,
            {"goal_text": "Маникюр", "source_channel": "miniapp"},
            format="json",
        )

        assert build_decision_context(customer)["missing"] == []
        run = GoalAnketaRun.objects.get(client=customer)
        assert run.completed_at is not None
        assert run.goal is not None

    def test_unrecognised_free_text_still_asks_for_clarification(
        self, customer, token,
    ):
        """OD-1 не отменён: в чём ничего не названо — то уточняется.

        Обратная сторона предыдущего теста. Если бы «готовой целью»
        считался любой текст, уточнение исчезло бы вообще, а вместе с
        ним и запрет OD-1 угадывать по близости.
        """
        api = _api()
        doc = api.post(
            SELECT_URL,
            {"goal_text": "хочу что-то для рук", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]
        assert _kinds(doc) == [MISSING_GOAL_CLARIFICATION]

    def test_a_way_out_exists_on_every_single_state(self, customer, token, catalog):
        """`next` есть ВСЕГДА — иначе анкета ворота, и не в теории.

        Поверхность цели монтируется на корне. Кнопки «назад» там нет
        (её там и не должно быть), нижней навигации у клиента нет тоже.
        Пока `next` молчал при непустом `missing`, уйти с экрана было
        нельзя иначе, чем создав цель. Это ворота — запрещено и
        решением владельца (C-2), и non-goal #1 BOT-001, который
        владелец НЕ отменял.

        Жёстче всего это било по тому, ради кого правка и делалась:
        «хочу маникюра» — родительный падеж, дословного совпадения с
        именем каталога нет, приходит `goal_clarification`, и человек,
        НАЗВАВШИЙ услугу, оказывался заперт на вопросе.

        DRF-1461 убрал сам этот случай: падежи распознаются, и «хочу
        маникюра» больше не роняет в уточнение. Проверять `next` на нём
        стало нечем — состояние, ради которого он был взят, исчезло.
        Поэтому шаг 3 теперь берёт фразу, которая уточнения заслуживает
        по существу («хочу что-то для рук» — услуга не названа), а
        прежний случай проверяется рядом отдельно: он обязан
        распознаваться, и выход при этом обязан остаться.
        """
        api = _api()

        # 1. Первый вопрос анкеты, цели нет вообще.
        doc = api.get(CTX_URL).json()["data"]
        assert doc["missing"], "предусловие: вопрос на экране есть"
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG

        # 2. Середина анкеты.
        first = anketa.ANKETA_STEPS[0]
        doc = _answer(api, first.key, option_key=first.options[0][0]).json()["data"]
        assert doc["missing"]
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG

        # 3. Услуга НЕ названа — уточнение, и выход рядом с ним.
        doc = api.post(
            SELECT_URL,
            {"goal_text": "хочу что-то для рук", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]
        assert _kinds(doc) == [MISSING_GOAL_CLARIFICATION]
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG, (
            "уточнение не должно быть тупиком"
        )

        # 3a. Тот самый падеж (DRF-1461): распознан, уточнения нет,
        # выход всё равно на месте.
        doc = api.post(
            SELECT_URL,
            {"goal_text": "хочу маникюра", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]
        assert doc["missing"] == [], "родительный падеж обязан распознаваться"
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG

        # 4. Состояние ведения.
        doc = api.post(
            SELECT_URL,
            {"intent": "need_guidance", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]
        assert _kinds(doc) == ["goal_guidance"]
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG

    def test_final_step_does_not_duplicate_its_own_chips_as_suggestions(
        self, customer, token, goal_options,
    ):
        """Финальный шаг сам несёт курируемые цели — второй ряд лишний.

        `suggestions` строятся из того же queryset. Оставить обе секции
        значило нарисовать два одинаковых ряда чипов с одинаковыми
        подписями. Выход при этом не теряется: чипы шага создают цель
        так же, и свободный ввод на финальном шаге открыт.
        """
        api = _api()
        for step in anketa.ANKETA_STEPS:
            _answer(api, step.key, option_key=step.options[0][0])
        doc = api.get(CTX_URL).json()["data"]

        assert doc["missing"][0]["step"] == anketa.FINAL_STEP_KEY
        assert [o["key"] for o in doc["missing"][0]["options"]] == ["relax", "glow"]
        assert doc["suggestions"] == []
        assert doc["missing"][0]["allow_free_text"] is True

    def test_named_service_matches_a_salon_service_too(
        self, customer, token, catalog,
    ):
        """Услуга салона — тоже названная услуга, если салон известен.

        DRF-1455: ``SalonService`` — прайс конкретного салона, и читается
        он только для салона клиента. Здесь салон назван заголовком
        ``X-Tenant`` — тем же способом, каким его называет всё
        остальное в этом бэкенде.
        """
        tenant = Tenant.objects.create(slug="penza-anketa", name="Penza")
        SalonService.objects.create(
            tenant=tenant, category=catalog, name="Аппаратный маникюр",
        )
        api = _api()
        api.defaults["HTTP_X_TENANT"] = tenant.slug
        doc = api.post(
            SELECT_URL,
            {"goal_text": "хочу аппаратный маникюр", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]
        assert doc["missing"] == []

    def test_salon_service_of_another_salon_is_not_a_named_service(
        self, customer, token, catalog,
    ):
        """Обратная половина той же проверки (DRF-1455).

        Та же услуга, тот же текст — но клиент действует в другом
        салоне. Услуга есть только у соседа, и «цель распознана» здесь
        было бы решением по чужим строкам.
        """
        other = Tenant.objects.create(slug="penza-anketa-b", name="Penza B")
        mine = Tenant.objects.create(slug="penza-anketa-a", name="Penza A")
        SalonService.objects.create(
            tenant=other, category=catalog, name="Аппаратный маникюр",
        )
        api = _api()
        api.defaults["HTTP_X_TENANT"] = mine.slug
        doc = api.post(
            SELECT_URL,
            {"goal_text": "хочу аппаратный маникюр", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]
        assert _kinds(doc) == [MISSING_GOAL_CLARIFICATION]
        assert doc["next"]["id"] == NEXT_BROWSE_CATALOG


# ---------------------------------------------------------------------------
# Прежние три пути DRF-1190 живы
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestOldPathsSurvive:
    def test_all_three_intents_are_offered_on_every_anketa_step(
        self, customer, token, goal_options,
    ):
        api = _api()
        doc = api.get(CTX_URL).json()["data"]
        for _ in anketa.ANKETA_STEPS:
            ids = [i["id"] for i in doc["intents"]]
            assert {"choose_suggested", "formulate_own", "need_guidance"} <= set(ids)
            assert doc["suggestions"], "чипы обязаны стоять рядом с вопросами"
            assert doc["next"]["id"] == NEXT_BROWSE_CATALOG, "выход обязан быть всегда"
            step = doc["missing"][0]["step"]
            expected = next(s for s in anketa.ANKETA_STEPS if s.key == step)
            doc = _answer(api, step, option_key=expected.options[0][0]).json()["data"]

    def test_need_guidance_still_answers_with_a_guiding_question(
        self, customer, token,
    ):
        api = _api()
        doc = api.post(
            SELECT_URL,
            {"intent": "need_guidance", "source_channel": "miniapp"},
            format="json",
        ).json()["data"]
        assert _kinds(doc) == ["goal_guidance"]
        assert ClientGoal.objects.count() == 0


# ---------------------------------------------------------------------------
# Повторный проход — DRF-1225 / условие C-4
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRepeatPass:
    def test_client_with_a_goal_is_offered_to_pass_again(
        self, customer, token, goal_options,
    ):
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="bot",
        )
        doc = _api().get(CTX_URL).json()["data"]
        assert doc["missing"] == []
        assert INTENT_START_ANKETA in [i["id"] for i in doc["intents"]]

    def test_start_anketa_reopens_the_questions_without_losing_the_goal(
        self, customer, token, goal_options,
    ):
        ClientGoal.objects.create(
            client=customer, goal_key="relax", source_channel="bot",
        )
        api = _api()
        doc = api.post(
            SELECT_URL,
            {"intent": INTENT_START_ANKETA, "source_channel": "miniapp"},
            format="json",
        ).json()["data"]

        assert doc["missing"][0]["step"] == anketa.ANKETA_STEPS[0].key
        # Начатый заново проход НЕ отменяет действующую цель: бросить
        # анкету на середине не значит остаться без цели.
        assert doc["known"]["goal"]["goal_key"] == "relax"
        # И повторно «пройти заново» уже не предлагается — человек в проходе.
        assert INTENT_START_ANKETA not in [i["id"] for i in doc["intents"]]

    def test_third_pass_is_allowed_too(self, customer, token, goal_options):
        api = _api()
        for expected_key in ("relax", "glow", "relax"):
            api.post(
                SELECT_URL,
                {"intent": INTENT_START_ANKETA, "source_channel": "miniapp"},
                format="json",
            )
            for step in anketa.ANKETA_STEPS:
                _answer(api, step.key, option_key=step.options[0][0])
            doc = _answer(
                api, anketa.FINAL_STEP_KEY, option_key=expected_key,
            ).json()["data"]
            assert doc["known"]["goal"]["goal_key"] == expected_key

        assert GoalAnketaRun.objects.filter(client=customer).count() == 3
        assert ClientGoal.objects.filter(client=customer, is_active=True).count() == 1

    def test_double_start_does_not_open_two_runs(self, customer, token):
        api = _api()
        for _ in range(2):
            resp = api.post(
                SELECT_URL,
                {"intent": INTENT_START_ANKETA, "source_channel": "miniapp"},
                format="json",
            )
            assert resp.status_code == 200
        assert GoalAnketaRun.objects.filter(
            client=customer, completed_at__isnull=True,
        ).count() == 1
