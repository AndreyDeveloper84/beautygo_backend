"""OD-1 — выбранная цель клиента влияет на пассивную выдачу.

Красный прогон этой задачи. До подключения резолвера все проверки с
флагом ON падают: `goals.resolution.resolve_goal_category_ids` боевых
вызывающих не имеет, `GOAL_RESOLUTION_ENABLED` не читается нигде, и
выдача целью не фильтруется.

Что здесь проверяется и почему именно так
-----------------------------------------
Правило контура: **отрицательному утверждению нужна положительная
стража на тех же данных.** Проверка «услуги вне цели не показаны»
проходит и на пустой выдаче, поэтому рядом с каждым таким assert стоит
«услуги внутри цели показаны». Оба мастера живут в одной фикстуре, с
одинаковым рейтингом и одинаковым адресом, — различает их только
категория услуги.

Фикстура воспроизводит форму пилота (DRF-1308): цель курируется на
КОРНЕ, а услуга висит на ЛИСТЕ. Значит зелёный тест доказывает заодно,
что раскрытие вниз по дереву доезжает до реальной выдачи, а не только
до юнита резолвера.

Оба положения флага проверяются симметрично: при OFF выдача обязана
остаться прежней — это главная гарантия безопасности выкладки, потому
что merge в `dev` бэкенда есть немедленная выкладка на боевой пилот.

Дат-литералов нет: всё время — смещения от `now`.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from ai.tests.factories import make_specialist, make_user
from goals.models import ClientGoal
from services.models import (
    GoalOption,
    GoalOptionCategory,
    Service,
    ServiceCategory,
)
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

HOME_URL = "/api/v1/home/"
CATALOG_URL = "/api/v1/internal/me/catalog/recommendations/"

VALID_TOKEN = "test-ayla-internal-token-od1"
EXTERNAL_USER_ID = "bot:od1"

# Общий адрес обоих мастеров: мягкий фильтр по городу не должен
# участвовать в различении — различает только категория.
COMMON_ADDRESS = "Penza, Lenina 1"


# ---------------------------------------------------------------------------
# Фикстуры: цель на КОРНЕ, услуга на ЛИСТЕ (форма пилота)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """Движок рекомендаций кэширует результат по `cache_key()`."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="od1-tenant", name="OD-1 Salon")


@pytest.fixture
def goal_root(db):
    return ServiceCategory.objects.create(name="Массаж тела", slug="od1-massage")


@pytest.fixture
def goal_leaf(db, goal_root):
    """Лист под корнем цели — здесь висит услуга (DRF-1308)."""
    return ServiceCategory.objects.create(
        name="Расслабляющие массажи", slug="od1-massage-relax", parent=goal_root,
    )


@pytest.fixture
def off_goal_category(db):
    return ServiceCategory.objects.create(name="Маникюр", slug="od1-manicure")


@pytest.fixture
def relax_option(db, goal_root):
    """Курируемая цель, связанная с КОРНЕМ — как на пилоте."""
    option = GoalOption.objects.create(key="relax", label="Расслабиться и снять стресс")
    GoalOptionCategory.objects.create(
        goal_option=option, category=goal_root, sort_order=0,
    )
    return option


@pytest.fixture
def in_goal_specialist(db, tenant, goal_leaf):
    """Мастер с услугой ВНУТРИ цели — положительная стража."""
    profile = make_specialist(display_name="В цели", address=COMMON_ADDRESS)
    profile.tenant = tenant
    profile.save()
    Service.objects.create(
        specialist=profile, category=goal_leaf, name="Расслабляющий массаж",
        price=Decimal("2000"), duration_minutes=60, is_active=True,
    )
    return profile


@pytest.fixture
def off_goal_specialist(db, tenant, off_goal_category):
    """Мастер с услугой ВНЕ цели — отрицательная проверка."""
    profile = make_specialist(display_name="Вне цели", address=COMMON_ADDRESS)
    profile.tenant = tenant
    profile.save()
    Service.objects.create(
        specialist=profile, category=off_goal_category, name="Маникюр",
        price=Decimal("1500"), duration_minutes=60, is_active=True,
    )
    return profile


@pytest.fixture
def both_specialists(in_goal_specialist, off_goal_specialist):
    return in_goal_specialist, off_goal_specialist


@pytest.fixture
def client_user(db):
    return make_user(role="client", city="Penza")


@pytest.fixture
def home_api(client_user):
    api = APIClient()
    api.defaults["HTTP_X_APP_TYPE"] = "client"
    api.force_authenticate(user=client_user)
    return api


@pytest.fixture
def bot_customer(db):
    """Прокси-пользователь бота: цель выбирается в боте / Mini App."""
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79994100001", is_proxy=True,
    )


@pytest.fixture
def catalog_api(bot_customer):
    api = APIClient()
    api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    api.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return api


def _select_goal(user, *, key="relax") -> ClientGoal:
    return ClientGoal.objects.create(
        client=user, goal_key=key, source_channel=ClientGoal.SourceChannel.BOT,
    )


def _home_names(api) -> set[str]:
    response = api.get(HOME_URL)
    assert response.status_code == 200, response.data
    return {
        row["display_name"] for row in response.data["data"]["nearby_specialists"]
    }


def _catalog_payload(api, **body) -> dict:
    response = api.post(CATALOG_URL, body, format="json")
    assert response.status_code == 200, response.data
    return response.data["data"]


def _layer_2_names(api, **body) -> set[str]:
    return {row["display_name"] for row in _catalog_payload(api, **body)["layer_2_ayla_picks"]}


# ---------------------------------------------------------------------------
# Пассивная выдача клиентского приложения — GET /api/v1/home/
# ---------------------------------------------------------------------------


class TestHomeFeedRespectsGoal:
    def test_goal_filters_home_feed_when_flag_on(
        self, settings, home_api, client_user, relax_option, both_specialists,
    ):
        """Ядро задачи: цель режет выдачу, и режет её правильно.

        Положительная стража и отрицательная проверка — на одних данных:
        мастер в цели показан, мастер вне цели скрыт. Одного второго
        assert было бы недостаточно: он проходит и на пустой выдаче.
        """
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(client_user)

        names = _home_names(home_api)

        assert "В цели" in names, "услуга внутри цели обязана остаться в выдаче"
        assert "Вне цели" not in names, "услуга вне цели не должна показываться"

    def test_flag_off_leaves_feed_untouched(
        self, settings, home_api, client_user, relax_option, both_specialists,
    ):
        """Главная гарантия выкладки: при OFF цель не влияет ни на что."""
        settings.GOAL_RESOLUTION_ENABLED = False
        _select_goal(client_user)

        assert _home_names(home_api) == {"В цели", "Вне цели"}

    def test_no_goal_keeps_previous_feed(
        self, settings, home_api, relax_option, both_specialists,
    ):
        """`None` (цели нет) — сегодняшняя норма для 100% пилота.

        ClientGoal на контуре = 0, поэтому эта ветка и есть поведение по
        умолчанию для всех. Она обязана остаться прежней выдачей, а не
        стать пустым экраном.
        """
        settings.GOAL_RESOLUTION_ENABLED = True

        assert _home_names(home_api) == {"В цели", "Вне цели"}

    def test_unmappable_goal_keeps_previous_feed(
        self, settings, home_api, client_user, both_specialists,
    ):
        """Цель есть, но связей нет → резолвер отдаёт `None`.

        Резолвер схлопывает «не разрешилось» и «разрешилось в ноль
        категорий» в один `None` (`_categories_for_option(...) or None`),
        поэтому вызывающая сторона обязана трактовать оба одинаково —
        и не имеет права показать пустой экран.
        """
        settings.GOAL_RESOLUTION_ENABLED = True
        GoalOption.objects.create(key="event", label="Собраться к событию")
        _select_goal(client_user, key="event")

        assert _home_names(home_api) == {"В цели", "Вне цели"}

    def test_free_text_goal_without_exact_match_keeps_previous_feed(
        self, settings, home_api, client_user, relax_option, both_specialists,
    ):
        """Свободный текст без точного совпадения — не угадываем (OD-1)."""
        settings.GOAL_RESOLUTION_ENABLED = True
        ClientGoal.objects.create(
            client=client_user, goal_text="хочу что-нибудь приятное",
            source_channel=ClientGoal.SourceChannel.BOT,
        )

        assert _home_names(home_api) == {"В цели", "Вне цели"}


# ---------------------------------------------------------------------------
# Пассивные полки Mini App — POST /internal/me/catalog/recommendations/
# ---------------------------------------------------------------------------


class TestCatalogRecommendationsRespectGoal:
    def test_goal_filters_layer_2_when_flag_on(
        self, settings, catalog_api, bot_customer, relax_option, both_specialists,
    ):
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(bot_customer)

        names = _layer_2_names(catalog_api)

        assert "В цели" in names, "мастер внутри цели обязан остаться в полке"
        assert "Вне цели" not in names

    def test_goal_filters_layer_3_categories_when_flag_on(
        self, settings, catalog_api, bot_customer, relax_option, both_specialists,
    ):
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(bot_customer)

        slugs = {
            row["slug"]
            for row in _catalog_payload(catalog_api)["layer_3_explore"]["categories"]
        }

        assert "od1-massage-relax" in slugs, "категория цели обязана остаться"
        assert "od1-manicure" not in slugs

    def test_flag_off_leaves_layers_untouched(
        self, settings, catalog_api, bot_customer, relax_option, both_specialists,
    ):
        settings.GOAL_RESOLUTION_ENABLED = False
        _select_goal(bot_customer)

        assert _layer_2_names(catalog_api) == {"В цели", "Вне цели"}

    def test_no_goal_keeps_previous_layers(
        self, settings, catalog_api, relax_option, both_specialists,
    ):
        settings.GOAL_RESOLUTION_ENABLED = True

        assert _layer_2_names(catalog_api) == {"В цели", "Вне цели"}

    def test_explicit_request_goal_outranks_stored_goal(
        self, settings, catalog_api, bot_customer, relax_option, both_specialists,
    ):
        """Сказанное сейчас важнее сохранённой цели.

        Клиент, набравший «маникюр», получает маникюр, даже если его
        сохранённая цель — «расслабиться». Контракт параметра `goal`
        не меняется: сохранённая цель работает только когда клиент
        молчит.
        """
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(bot_customer)

        names = _layer_2_names(catalog_api, goal="Маникюр")

        assert "Вне цели" in names, "явный запрос обязан победить сохранённую цель"
        assert "В цели" not in names


# ---------------------------------------------------------------------------
# Кэш движка: цель обязана входить в ключ
# ---------------------------------------------------------------------------


class TestFlagOffCostsNothing:
    def test_flag_off_does_not_touch_the_database(
        self, settings, client_user, relax_option,
    ):
        """При OFF подключение не делает даже запроса.

        Гарантия выкладки сильнее сравнения ответов: выключенный флаг
        не добавляет к пути выдачи ни одного обращения к БД, поэтому
        измениться не может ни поведение, ни бюджет запросов.
        """
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        from goals.wiring import goal_category_ids_for

        settings.GOAL_RESOLUTION_ENABLED = False
        _select_goal(client_user)

        with CaptureQueriesContext(connection) as captured:
            assert goal_category_ids_for(client_user) is None

        assert len(captured) == 0, "выключенный флаг не должен ходить в БД"


class TestRecommendationCacheKey:
    def test_goal_categories_change_cache_key(self, db, goal_root, goal_leaf):
        """Иначе выдача одной цели утекла бы клиенту с другой целью."""
        from ai.application.services.recommendation_engine import RecommendationQuery

        base = RecommendationQuery(client_id=None)
        with_goal = RecommendationQuery(
            client_id=None, goal_category_ids=(goal_root.id, goal_leaf.id),
        )
        other_goal = RecommendationQuery(
            client_id=None, goal_category_ids=(goal_leaf.id,),
        )

        assert base.cache_key() != with_goal.cache_key()
        assert with_goal.cache_key() != other_goal.cache_key()

    def test_absent_goal_leaves_cache_key_unchanged(self, db):
        """Флаг OFF не должен менять даже ключ кэша."""
        from ai.application.services.recommendation_engine import RecommendationQuery

        assert (
            RecommendationQuery(client_id=None).cache_key()
            == RecommendationQuery(client_id=None, goal_category_ids=None).cache_key()
        )
