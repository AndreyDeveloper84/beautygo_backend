"""OD-1 — выбранная цель клиента влияет на пассивную выдачу.

Фильтр идёт по КАНОНИЧЕСКОМУ каталогу (`SalonService` +
`SpecialistService`), а не по легаси-модели `Service`.

Почему фикстуры выглядят именно так
-----------------------------------
Замер боевого пилота 2026-08-29:

    SpecialistService   292
    SalonService         94   (у всех 94 категория заполнена)
    Service (легаси)      0
    Review                0

Легаси-таблица пуста ЦЕЛИКОМ — это не «часть строк не размечена», а ноль
строк: `Service` наполняется только вручную через Pro-приложение, а
пилотный каталог заезжал интейком в канонический слой (незавершённая
strangler-fig миграция, чанк S3-CUT). Поэтому базовые фикстуры здесь
**не создают ни одной легаси-строки** — ровно как на пилоте. Первый
заход этой задачи фильтровал по `Service` и на боевых данных отдал бы
пустую полку каждому, кто выбрал любую из семи целей.

Правило контура: **отрицательному утверждению нужна положительная
стража на тех же данных.** Рядом с «услуги вне цели не показаны» стоит
«услуги внутри цели показаны», и отдельно — счётный тест
`test_goal_finds_services_the_legacy_filter_could_not`, который требует
НЕНУЛЕВОГО числа при пустой легаси-таблице. Проверка на пустой выдаче
проходит всегда и не значит ничего.

Оба положения флага проверяются симметрично: при OFF поведение обязано
остаться прежним — merge в `dev` есть немедленная выкладка на боевой
пилот с живыми людьми.

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
    SalonService,
    Service,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

HOME_URL = "/api/v1/home/"
CATALOG_URL = "/api/v1/internal/me/catalog/recommendations/"

VALID_TOKEN = "test-ayla-internal-token-od1"
EXTERNAL_USER_ID = "bot:od1"

# Общий адрес у всех мастеров: мягкий фильтр по городу не должен
# участвовать в различении — различает только категория.
COMMON_ADDRESS = "Penza, Lenina 1"


# ---------------------------------------------------------------------------
# Фикстуры
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
    """Лист под корнем цели — здесь живут услуги (DRF-1308)."""
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


def _canonical(profile, tenant, *, name, category=None, template=None):
    """Каноническая связка: SalonService + бронируемый SpecialistService."""
    salon = SalonService.objects.create(
        tenant=tenant, category=category, template=template, name=name,
    )
    SpecialistService.objects.create(
        salon_service=salon, specialist=profile,
        price=Decimal("2000"), duration_minutes=60,
    )
    return salon


def _specialist(display_name):
    return make_specialist(display_name=display_name, address=COMMON_ADDRESS)


@pytest.fixture
def in_goal_specialist(db, tenant, goal_leaf):
    """Услуга ВНУТРИ цели, на листе под курируемым корнем."""
    profile = _specialist("В цели")
    _canonical(profile, tenant, name="Расслабляющий массаж", category=goal_leaf)
    return profile


@pytest.fixture
def off_goal_specialist(db, tenant, off_goal_category):
    """Услуга ВНЕ цели — отрицательная проверка."""
    profile = _specialist("Вне цели")
    _canonical(profile, tenant, name="Маникюр", category=off_goal_category)
    return profile


@pytest.fixture
def template_specialist(db, tenant, goal_leaf):
    """Категория не проставлена — цель обязана доехать через шаблон.

    На пилоте сегодня категория есть у всех 94 услуг, но
    `SalonService.category` обнуляем по схеме, а
    `ServiceTemplate.category` — NOT NULL. Тест строится на схеме, а не
    на сегодняшнем состоянии данных.
    """
    template = ServiceTemplate.objects.create(
        category=goal_leaf, name="Стоун-массаж", name_short="Стоун",
        duration_default=60,
    )
    profile = _specialist("Через шаблон")
    _canonical(profile, tenant, name="Стоун-массаж", template=template)
    return profile


@pytest.fixture
def miscategorized_specialist(db, tenant, goal_leaf, off_goal_category):
    """Своя категория ВНЕ цели, а шаблон — внутри.

    Салон явно отнёс услугу к своей категории, и это решение
    приоритетнее шаблона: шаблон — запасной путь, а не объединение
    (DRF-1308 п.1 и п.4 — не приписывать цель, которой владелец для
    услуги не заявлял).
    """
    template = ServiceTemplate.objects.create(
        category=goal_leaf, name="Массаж рук", name_short="Массаж рук",
        duration_default=30,
    )
    profile = _specialist("Своя категория важнее")
    _canonical(
        profile, tenant, name="Массаж рук в пакете",
        category=off_goal_category, template=template,
    )
    return profile


@pytest.fixture
def both_specialists(in_goal_specialist, off_goal_specialist):
    return in_goal_specialist, off_goal_specialist


@pytest.fixture
def legacy_mirror(
    db, in_goal_specialist, off_goal_specialist, goal_leaf, off_goal_category,
):
    """Легаси-строки ДОПОЛНИТЕЛЬНО к каноническим.

    Нужны только там, где проверяемая машинерия сама читает легаси
    (`_build_layer_3`, ILIKE-фильтр по `services__name`). На пилоте
    таких строк нет — см. докстринг модуля.
    """
    Service.objects.create(
        specialist=in_goal_specialist, category=goal_leaf,
        name="Расслабляющий массаж", price=Decimal("2000"),
        duration_minutes=60, is_active=True,
    )
    Service.objects.create(
        specialist=off_goal_specialist, category=off_goal_category,
        name="Маникюр", price=Decimal("1500"),
        duration_minutes=60, is_active=True,
    )


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
    return {
        row["display_name"]
        for row in _catalog_payload(api, **body)["layer_2_ayla_picks"]
    }


# ---------------------------------------------------------------------------
# Ядро перевода: канонический каталог находит то, чего не находил легаси
# ---------------------------------------------------------------------------


class TestCanonicalCatalogIsTheSourceOfTruth:
    def test_goal_finds_services_the_legacy_filter_could_not(
        self, settings, home_api, client_user, relax_option,
        in_goal_specialist, template_specialist, off_goal_specialist,
    ):
        """Счётная проверка на форме пилота: легаси пуст, канон — нет.

        Именно этот тест ловит регресс первого захода: фильтр по
        `Service` на таких данных отдавал НОЛЬ мастеров, то есть пустую
        полку человеку, выбравшему цель. Требуем ненулевое число, а не
        «ничего не сломалось».
        """
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(client_user)

        assert Service.objects.count() == 0, "фикстура обязана повторять пилот"

        names = _home_names(home_api)

        assert len(names) == 2, f"цель обязана найти услуги в каноне, получено: {names}"
        assert names == {"В цели", "Через шаблон"}

    def test_template_category_is_the_fallback_when_salon_left_it_empty(
        self, settings, home_api, client_user, relax_option,
        template_specialist, off_goal_specialist,
    ):
        """`SalonService.category` обнуляем по схеме — шаблон подстрахует."""
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(client_user)

        names = _home_names(home_api)

        assert "Через шаблон" in names
        assert "Вне цели" not in names

    def test_salon_own_category_outranks_template(
        self, settings, home_api, client_user, relax_option,
        miscategorized_specialist, in_goal_specialist,
    ):
        """Шаблон — запасной путь, а не объединение.

        Услуга, которую салон явно отнёс к своей категории вне цели, не
        должна попасть в цель через шаблон: это была бы цель, которой
        владелец для неё не заявлял (DRF-1308 п.4).
        """
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(client_user)

        names = _home_names(home_api)

        assert "В цели" in names, "положительная стража"
        assert "Своя категория важнее" not in names


# ---------------------------------------------------------------------------
# Пассивная выдача клиентского приложения — GET /api/v1/home/
# ---------------------------------------------------------------------------


class TestHomeFeedRespectsGoal:
    def test_goal_filters_home_feed_when_flag_on(
        self, settings, home_api, client_user, relax_option, both_specialists,
    ):
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
        """`None` (цели нет) — сегодняшняя норма для 100% пилота."""
        settings.GOAL_RESOLUTION_ENABLED = True

        assert _home_names(home_api) == {"В цели", "Вне цели"}

    def test_unmappable_goal_keeps_previous_feed(
        self, settings, home_api, client_user, both_specialists,
    ):
        """Цель есть, связей нет → резолвер отдаёт `None`.

        Резолвер схлопывает «не разрешилось» и «разрешилось в ноль
        категорий» в один `None`, поэтому оба случая обязаны вести к
        прежней выдаче, а не к пустому экрану.
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

    def test_inactive_bookable_link_does_not_expose_the_service(
        self, settings, home_api, client_user, relax_option,
        in_goal_specialist, off_goal_specialist,
    ):
        """Снятая с публикации связка не должна возвращаться через цель.

        Две фазы на одних данных: сперва мастер ВИДЕН по цели, потом
        связка гасится и он пропадает. Без первой фазы проверка
        выродилась бы в «пусто до и пусто после» и проходила бы даже
        при полностью сломанном фильтре.
        """
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(client_user)

        assert "В цели" in _home_names(home_api), "положительная стража до правки данных"

        SpecialistService.objects.filter(specialist=in_goal_specialist).update(
            is_active=False,
        )
        from django.core.cache import cache
        cache.clear()

        assert _home_names(home_api) == set()


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
        self, settings, catalog_api, bot_customer, relax_option,
        both_specialists, legacy_mirror,
    ):
        """Фильтр пула по цели доезжает до полки 3.

        ВАЖНО: сама полка 3 всё ещё считает категории по ЛЕГАСИ-таблице
        (`ServiceCategory.services`), поэтому на пилоте она пуста
        независимо от цели — пред-существующий дефект, зафиксирован в
        описании PR и здесь не чинится. Фикстура `legacy_mirror` для
        того и нужна: тест проверяет ФИЛЬТРАЦИЮ ПУЛА, а не то, что
        полка работает на боевых данных.
        """
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
        self, settings, catalog_api, bot_customer, relax_option,
        both_specialists, legacy_mirror,
    ):
        """Сказанное сейчас важнее сохранённой цели.

        Явный `goal` идёт прежним ILIKE-путём по легаси-таблице, поэтому
        фикстура добавляет легаси-строки: контракт параметра не менялся.
        """
        settings.GOAL_RESOLUTION_ENABLED = True
        _select_goal(bot_customer)

        names = _layer_2_names(catalog_api, goal="Маникюр")

        assert "Вне цели" in names, "явный запрос обязан победить сохранённую цель"
        assert "В цели" not in names


# ---------------------------------------------------------------------------
# Выключенный флаг не стоит ничего
# ---------------------------------------------------------------------------


class TestFlagOffCostsNothing:
    def test_flag_off_does_not_touch_the_database(
        self, settings, client_user, relax_option,
    ):
        """При OFF подключение не делает даже запроса.

        Гарантия сильнее сравнения ответов: выключенный флаг не
        добавляет к пути выдачи ни одного обращения к БД.
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
