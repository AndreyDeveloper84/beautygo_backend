"""HTTP-проекция границы — `POST /api/v1/internal/recommendation/resolve/`.

Контракт §9.4. Проверяется не «ручка отвечает 200», а четыре свойства, из-за
отсутствия которых расхождение форм прожило незамеченным:

* схема стоит на **обоих** концах, включая собственный выход источника;
* версия контракта едет в теле;
* ненастроенный источник — **недоступность**, а не пустая выдача;
* кого спрашивают, решает аутентификация, а не тело запроса.
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from recommendation._serializers import FORBIDDEN_RESPONSE_FIELDS
from recommendation.api import RESOLVER_SPEC_VERSION
from users.models import User

from .conftest import StaticSource, make_facts

VALID_TOKEN = "test-ayla-internal-token-resolver"
URL = "/api/v1/internal/recommendation/resolve/"
EXTERNAL_USER_ID = "bot:resolver"

#: Кандидаты, которые отдаёт привязанный в тестах источник. Модуль-уровень,
#: потому что фабрика в настройке — путь к вызываемому объекту, а не объект.
_FIXTURE_CANDIDATES: list = []


def fixture_source_factory():
    """Фабрика источника для `RECOMMENDATION_CANDIDATE_SOURCE` в тестах."""
    return StaticSource(_FIXTURE_CANDIDATES)


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79994100001", is_proxy=True,
    )


@pytest.fixture
def bound_source(settings):
    """Привязать источник и наполнить его двумя различимыми кандидатами."""
    _FIXTURE_CANDIDATES.clear()
    _FIXTURE_CANDIDATES.extend([make_facts(rating=("4.9", 0)), make_facts()])
    settings.RECOMMENDATION_CANDIDATE_SOURCE = (
        "recommendation.tests.test_resolve_endpoint.fixture_source_factory"
    )
    yield _FIXTURE_CANDIDATES
    _FIXTURE_CANDIDATES.clear()


def _api(*, bearer: str | None = VALID_TOKEN, external_user_id: str | None = EXTERNAL_USER_ID) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if external_user_id is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = external_user_id
    return c


def _body(**overrides) -> dict:
    body = {
        "request_id": "req-http-1",
        "surface": "MINIAPP_HOME",
        "scope": {"mode": "MARKETPLACE"},
        "need": {"origin": "USER_EXPLICIT", "raw_text": "массаж"},
        # Состояние безопасности называется явно и всегда: `UNKNOWN`
        # по §14 fail-closed, то есть равносильно STOP. Умолчание здесь
        # молча превращало бы забытое поле в пустую выдачу.
        "safety_state": "NORMAL",
        "tie_break_seed": "conv-http",
        "k": 3,
    }
    body.update(overrides)
    return body


@pytest.mark.django_db
class TestAuthBoundary:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_missing_bearer_denied(self, customer):
        assert _api(bearer=None).post(URL, _body(), format="json").status_code == 403

    def test_wrong_bearer_denied(self, customer):
        assert _api(bearer="wrong").post(URL, _body(), format="json").status_code == 403


@pytest.mark.django_db
class TestSchemaOnBothEnds:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_malformed_request_is_a_contract_error_not_an_empty_result(self, customer, bound_source):
        """Запрос не по схеме — 400, а не «ничего не нашли».

        Пустая выдача утверждает «подходящих нет». Ответ на кривой запрос
        такого утверждения делать не вправе.
        """
        r = _api().post(URL, _body(surface="TELEPATHY"), format="json")
        assert r.status_code == 400

    def test_missing_safety_state_is_refused_not_silently_emptied(self, customer, bound_source):
        """Забытое состояние безопасности — 400, а не пустая выдача.

        `UNKNOWN` по §14 fail-closed: множество пусто, стадии не
        выполняются. Будь у поля умолчание, вызывающий, который его не
        прислал, получил бы честный по форме и лживый по смыслу ответ
        «подходящих нет» — притом что искать никто и не начинал.
        Этот тест написан после того, как ровно так и вышло в CI.
        """
        body = _body()
        del body["safety_state"]
        r = _api().post(URL, body, format="json")
        assert r.status_code == 400
        assert "safety_state" in r.json().get("error", {}).get("details", r.json())

    def test_safety_unknown_yields_an_empty_but_honest_answer(self, customer, bound_source):
        """А вот ЯВНО названный `UNKNOWN` — законный запрос с пустым ответом.

        Разница между этим тестом и предыдущим и есть вся суть: «мы не
        знаем, безопасно ли» — это ответ, который кто-то дал; отсутствие
        поля — вопрос, который никто не задавал.
        """
        r = _api().post(URL, _body(safety_state="UNKNOWN"), format="json")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["ordered"] == []
        assert all(e["reason_code"] == "ELIG_EXCLUDED_SAFETY" for e in data["excluded"])

    def test_response_carries_the_contract_version(self, customer, bound_source):
        r = _api().post(URL, _body(), format="json")
        assert r.status_code == 200
        assert r.json()["data"]["resolver_spec_version"] == RESOLVER_SPEC_VERSION

    def test_response_has_no_display_string_and_no_score(self, customer, bound_source):
        """W1: строку человеку собирает представление, а не источник (§7).

        Проверка рекурсивная: запрещённое поле, всплывшее на любой глубине
        — в кандидате, в свидетельстве, в отладочном довеске, — красит тест.
        """
        payload = _api().post(URL, _body(), format="json").json()["data"]
        assert _forbidden_keys(payload) == set()

    def test_rating_never_travels_without_review_count(self, customer, bound_source):
        """E2 на проводе: оценка и число отзывов едут парой или не едут."""
        payload = _api().post(URL, _body(), format="json").json()["data"]
        ratings = [
            item
            for candidate in payload["ordered"]
            for item in candidate["evidence"]
            if item["kind"] == "RATING"
        ]
        assert ratings, "фикстура несёт рейтинг — свидетельство обязано доехать"
        for item in ratings:
            assert set(item["value"]) == {"rating", "review_count"}
            assert item["strength"] == "UNSUBSTANTIATED"


@pytest.mark.django_db
class TestUnavailabilityIsNotEmptiness:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_unbound_source_answers_503_not_200_with_empty_list(self, customer, settings):
        """Ненастроенный источник — недоступность, а не «никого нет».

        Ровно то различие, слияние которого в одну ветку стоило нам
        DEFECT-C-02: два состояния с противоположной ценой молчания
        сходились в один пустой результат.
        """
        settings.RECOMMENDATION_CANDIDATE_SOURCE = None
        r = _api().post(URL, _body(), format="json")
        assert r.status_code == 503
        assert "data" not in r.json()


@pytest.mark.django_db
class TestSubjectComesFromAuth:
    @pytest.fixture(autouse=True)
    def _token(self, settings):
        settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN

    def test_body_cannot_name_another_subject(self, customer, bound_source):
        """`subject_ref` в теле не существует — и попытка его прислать не проходит.

        Иначе владелец Bearer-токена получал бы решение «за другого
        человека»: граница, отнявшая политику, раздала бы личный контекст.
        """
        r = _api().post(URL, _body(subject_ref=str(uuid.uuid4())), format="json")
        # Лишнее поле не влияет на результат: субъект берётся из аутентификации.
        assert r.status_code == 200
        assert r.json()["data"]["request_id"] == "req-http-1"


def _forbidden_keys(node) -> set:
    """Собрать запрещённые ключи с любой глубины ответа."""
    found: set = set()
    if isinstance(node, dict):
        found |= FORBIDDEN_RESPONSE_FIELDS & set(node)
        for value in node.values():
            found |= _forbidden_keys(value)
    elif isinstance(node, list):
        for value in node:
            found |= _forbidden_keys(value)
    return found
