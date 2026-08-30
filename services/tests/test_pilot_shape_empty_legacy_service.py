"""Боевые поверхности на форме пилота: легаси `Service` пуст целиком.

Замер боевого пилота (прямой запрос к `dev-web-1`, 2026-08-30):

    SpecialistService   292
    SalonService         94
    Service (легаси)      0
    Review                0

`Service` пуст не «частично» — ноль строк. Это остаток незавершённой
strangler-fig миграции (чанк S3-CUT): приёмка пилота пишет
`SalonService` + `SpecialistService` и легаси-таблицу не трогает вовсе.
Каждая поверхность, читающая `Service`, поэтому молча отдаёт пустоту —
и выглядит это как «ничего не нашлось», а не как поломка. Именно
поэтому никто не пожаловался.

Фикстуры повторяют форму пилота буквально: **ни одной легаси-строки**.
Каждый тест сначала утверждает `Service.objects.count() == 0`, чтобы
случайно заведённая легаси-строка не «починила» проверку.

Правило контура: **отрицательному утверждению нужна положительная
стража на тех же данных.** Проверка «пусто, значит не сломалось»
проходит всегда и не значит ничего, поэтому здесь везде требуется
НЕНУЛЕВОЕ число: столько-то услуг, столько-то мастеров.

Запасной путь через `ServiceTemplate.category` — именно запасной, а не
объединение: своя категория салона побеждает (DRF-1308 п.1 и п.4).
Отдельный тест это фиксирует.

Никаких литеральных дат — только смещения от `now`.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from ai.tests.factories import make_specialist, make_user
from services.models import (
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
SEARCH_URL = "/api/v1/search/"
SPECIALISTS_URL = "/api/v1/specialists/"

VALID_TOKEN = "test-ayla-internal-token-s3empty"
EXTERNAL_USER_ID = "bot:s3empty"

COMMON_ADDRESS = "Penza, Lenina 1"

MASSAGE = "Расслабляющий массаж"
MANICURE = "Классический маникюр"


# ---------------------------------------------------------------------------
# Фикстуры — форма пилота: канон наполнен, легаси пуст
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    """`_popular_categories` и движок рекомендаций кэшируют результат."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="s3empty-tenant", name="S3 Empty Salon")


@pytest.fixture
def massage_category(db):
    return ServiceCategory.objects.create(name="Массаж тела", slug="s3e-massage")


@pytest.fixture
def manicure_category(db):
    return ServiceCategory.objects.create(name="Маникюр", slug="s3e-manicure")


def _canonical(profile, tenant, *, name, category=None, template=None,
               price=Decimal("2000"), duration=60):
    """Каноническая связка: SalonService + бронируемый SpecialistService."""
    salon = SalonService.objects.create(
        tenant=tenant, category=category, template=template, name=name,
        duration_minutes=duration,
    )
    link = SpecialistService.objects.create(
        salon_service=salon, specialist=profile,
        price=price, duration_minutes=duration,
    )
    return salon, link


def _specialist(display_name, tenant):
    profile = make_specialist(display_name=display_name, address=COMMON_ADDRESS)
    profile.tenant = tenant
    profile.save(update_fields=["tenant"])
    return profile


@pytest.fixture
def massage_master(db, tenant, massage_category):
    profile = _specialist("Ирина П.", tenant)
    salon, link = _canonical(
        profile, tenant, name=MASSAGE, category=massage_category,
    )
    profile.salon_service = salon
    profile.specialist_service = link
    return profile


@pytest.fixture
def manicure_master(db, tenant, manicure_category):
    profile = _specialist("Ольга К.", tenant)
    _canonical(profile, tenant, name=MANICURE, category=manicure_category)
    return profile


@pytest.fixture
def template_master(db, tenant, massage_category):
    """Своя категория не проставлена — категория обязана доехать через шаблон.

    `SalonService.category` обнуляем по схеме (обязателен только когда
    нет `template`), а `ServiceTemplate.category` — NOT NULL. Без
    запасного пути такая услуга выпала бы из категорийных поверхностей
    молча.
    """
    template = ServiceTemplate.objects.create(
        category=massage_category, name="Стоун-массаж", name_short="Стоун",
        duration_default=60,
    )
    profile = _specialist("Дарья Ш.", tenant)
    _canonical(profile, tenant, name="Стоун-массаж", template=template)
    return profile


@pytest.fixture
def miscategorized_master(db, tenant, massage_category, manicure_category):
    """Своя категория — «Маникюр», шаблон — «Массаж тела».

    Шаблон читается ТОЛЬКО когда своей категории нет вовсе. Салон явно
    отнёс услугу к своей категории, и приписывать ей категорию шаблона
    значило бы приписать салону то, чего он не объявлял.
    """
    template = ServiceTemplate.objects.create(
        category=massage_category, name="Массаж рук", name_short="Массаж рук",
        duration_default=30,
    )
    profile = _specialist("Полина К.", tenant)
    _canonical(
        profile, tenant, name="Массаж рук в пакете",
        category=manicure_category, template=template,
    )
    return profile


@pytest.fixture
def client_user(db):
    return make_user(role="client", city="Penza")


@pytest.fixture
def app_api(client_user):
    """Клиентское приложение: JWT + X-App-Type."""
    api = APIClient()
    api.defaults["HTTP_X_APP_TYPE"] = "client"
    api.force_authenticate(user=client_user)
    return api


@pytest.fixture
def bot_customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79994200001", is_proxy=True,
    )


@pytest.fixture
def catalog_api(bot_customer):
    api = APIClient()
    api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    api.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return api


def _assert_pilot_shape():
    """Страж фикстуры: легаси обязан быть пуст, канон — нет."""
    assert Service.objects.count() == 0, (
        "фикстура обязана повторять пилот: ни одной легаси-строки"
    )
    assert SpecialistService.objects.count() > 0, (
        "фикстура обязана повторять пилот: канонические связки есть"
    )


def _catalog(api, **body) -> dict:
    response = api.post(CATALOG_URL, body, format="json")
    assert response.status_code == 200, response.data
    return response.data["data"]


# ---------------------------------------------------------------------------
# Посылка 1 — слой «Исследовать» (layer_3_explore) всегда пуст
# ---------------------------------------------------------------------------


class TestLayer3Explore:
    def test_explore_layer_counts_canonical_catalog(
        self, catalog_api, massage_master, manicure_master,
    ):
        """Полка «Исследовать» обязана назвать НЕНУЛЕВОЕ число категорий.

        `_build_layer_3` считает через `ServiceCategory.services` —
        обратную связь легаси `Service`. На пилоте это ноль строк, то
        есть пустая полка у каждого клиента.
        """
        _assert_pilot_shape()

        data = _catalog(catalog_api)

        # Положительная стража: пул не пуст — падать обязан именно
        # подсчёт категорий, а не сама выборка мастеров.
        assert len(data["layer_2_ayla_picks"]) == 2, data["layer_2_ayla_picks"]

        categories = data["layer_3_explore"]["categories"]
        by_slug = {row["slug"]: row for row in categories}
        assert len(categories) == 2, categories
        assert by_slug["s3e-massage"]["count"] == 1
        assert by_slug["s3e-manicure"]["count"] == 1

    def test_explore_layer_falls_back_to_template_category(
        self, catalog_api, template_master,
    ):
        """Своя категория пуста — категорию даёт шаблон (запасной путь)."""
        _assert_pilot_shape()

        categories = _catalog(catalog_api)["layer_3_explore"]["categories"]

        assert [row["slug"] for row in categories] == ["s3e-massage"], categories
        assert categories[0]["count"] == 1

    def test_explore_layer_prefers_salon_category_over_template(
        self, catalog_api, miscategorized_master,
    ):
        """Шаблон — запасной путь, а не объединение.

        Услуга с явной категорией «Маникюр» обязана считаться маникюром,
        даже когда её шаблон висит на «Массаж тела».
        """
        _assert_pilot_shape()

        categories = _catalog(catalog_api)["layer_3_explore"]["categories"]

        assert [row["slug"] for row in categories] == ["s3e-manicure"], categories


# ---------------------------------------------------------------------------
# Посылка 2 — любая непустая строка goal даёт пустой layer_2
# ---------------------------------------------------------------------------


class TestExplicitGoalFilter:
    def test_goal_by_service_name_keeps_matching_master(
        self, catalog_api, massage_master, manicure_master,
    ):
        """Явный `goal` ILIKE-ит `services__name` — легаси, то есть ничего.

        Отрицательное утверждение («маникюрщица не показана») стоит
        рядом с положительным («массажист показан») на одних данных.
        """
        _assert_pilot_shape()

        names = {
            row["display_name"]
            for row in _catalog(catalog_api, goal="массаж")["layer_2_ayla_picks"]
        }

        assert names == {"Ирина П."}, names

    def test_goal_by_category_name_keeps_matching_master(
        self, catalog_api, massage_master, manicure_master,
    ):
        _assert_pilot_shape()

        names = {
            row["display_name"]
            for row in _catalog(catalog_api, goal="Маникюр")["layer_2_ayla_picks"]
        }

        assert names == {"Ольга К."}, names

    def test_goal_reasoning_text_names_the_match(
        self, catalog_api, massage_master,
    ):
        """`_goal_matches` тоже ходит в легаси — совпадение не называется."""
        _assert_pilot_shape()

        rows = _catalog(catalog_api, goal="массаж")["layer_2_ayla_picks"]

        assert len(rows) == 1, rows
        assert "Совпадает с твоей целью" in rows[0]["reasoning_text"]

    def test_goal_falls_back_to_template_category(
        self, catalog_api, template_master, manicure_master,
    ):
        _assert_pilot_shape()

        names = {
            row["display_name"]
            for row in _catalog(catalog_api, goal="Массаж тела")["layer_2_ayla_picks"]
        }

        assert names == {"Дарья Ш."}, names

    def test_goal_prefers_salon_category_over_template(
        self, catalog_api, miscategorized_master, massage_master,
    ):
        """Своя категория побеждает: по «Массаж тела» приходит только тот,
        кто действительно в этой категории."""
        _assert_pilot_shape()

        names = {
            row["display_name"]
            for row in _catalog(catalog_api, goal="Массаж тела")["layer_2_ayla_picks"]
        }

        assert names == {"Ирина П."}, names


# ---------------------------------------------------------------------------
# Посылка 3 — превью услуг специалиста пустые
# ---------------------------------------------------------------------------


class TestSpecialistServicesPreview:
    def test_catalog_list_preview_is_not_empty(self, app_api, massage_master):
        """`GET /api/v1/specialists/` — карточка мастера в каталоге."""
        _assert_pilot_shape()

        response = app_api.get(SPECIALISTS_URL)
        assert response.status_code == 200, response.data
        rows = response.data["results"]
        row = next(r for r in rows if r["id"] == str(massage_master.id))

        assert row["services_count"] == 1, row
        assert len(row["services_preview"]) == 1, row["services_preview"]
        assert row["services_preview"][0]["name"] == MASSAGE

    def test_catalog_detail_lists_services(self, app_api, massage_master):
        """`GET /api/v1/specialists/{id}/` — профиль мастера."""
        _assert_pilot_shape()

        response = app_api.get(f"{SPECIALISTS_URL}{massage_master.id}/")
        assert response.status_code == 200, response.data

        names = [row["name"] for row in response.data["services"]]
        assert names == [MASSAGE], response.data["services"]

    def test_services_action_lists_services(self, app_api, massage_master):
        """`GET /api/v1/specialists/{id}/services/` — список для записи."""
        _assert_pilot_shape()

        response = app_api.get(f"{SPECIALISTS_URL}{massage_master.id}/services/")
        assert response.status_code == 200, response.data

        names = [row["name"] for row in response.data]
        assert names == [MASSAGE], response.data

    def test_services_action_exposes_the_bookable_id(
        self, app_api, massage_master,
    ):
        """Отданный id обязан быть тем, который принимает бронирование.

        Для канонического каталога это `SalonService.id` — ключ, который
        разбирает `services.service_resolver.resolve_bookable_service`
        (он же лежит в `Appointment.salon_service`).
        """
        _assert_pilot_shape()

        response = app_api.get(f"{SPECIALISTS_URL}{massage_master.id}/services/")

        assert response.data[0]["id"] == str(massage_master.salon_service.id)

    def test_home_nearby_specialists_preview_is_not_empty(
        self, app_api, massage_master,
    ):
        """`GET /api/v1/home/` — полка «рядом с вами»."""
        _assert_pilot_shape()

        response = app_api.get(HOME_URL)
        assert response.status_code == 200, response.data
        rows = response.data["data"]["nearby_specialists"]

        row = next(r for r in rows if r["id"] == str(massage_master.id))
        assert row["services_preview"] == [MASSAGE], row


# ---------------------------------------------------------------------------
# Посылка 4 — полнотекстовый поиск по услугам не находит ничего
# ---------------------------------------------------------------------------


class TestGlobalSearch:
    def test_search_finds_services(self, app_api, massage_master, manicure_master):
        """`GET /api/v1/search/?q=…` — раздел «услуги»."""
        _assert_pilot_shape()

        response = app_api.get(f"{SEARCH_URL}?q=массаж")
        assert response.status_code == 200, response.data
        services = response.data["data"]["services"]

        assert len(services) == 1, services
        assert services[0]["name"] == MASSAGE
        assert services[0]["specialist_name"] == "Ирина П."

    def test_search_finds_specialists_by_service_name(
        self, app_api, massage_master, manicure_master,
    ):
        """Мастера ищут по названию услуги — тоже через легаси-join."""
        _assert_pilot_shape()

        response = app_api.get(f"{SEARCH_URL}?q=массаж")
        assert response.status_code == 200, response.data
        names = {row["display_name"] for row in response.data["data"]["specialists"]}

        assert names == {"Ирина П."}, names

    def test_search_specialist_preview_is_not_empty(
        self, app_api, massage_master,
    ):
        _assert_pilot_shape()

        response = app_api.get(f"{SEARCH_URL}?q=Ирина")
        rows = response.data["data"]["specialists"]

        assert len(rows) == 1, rows
        assert [s["name"] for s in rows[0]["services_preview"]] == [MASSAGE]


# ---------------------------------------------------------------------------
# Посылка 5 — ai/tools_handlers резолвит Service без запасного пути
# ---------------------------------------------------------------------------


class TestConciergeToolHandlers:
    def test_show_slots_resolves_canonical_service(self, massage_master):
        """`show_slots` обязан разобрать канонический id, а не уточнять."""
        from ai.tools import ActionType
        from ai.tools_handlers import handle_show_slots

        _assert_pilot_shape()
        target = (timezone.localtime() + timedelta(days=1)).date()

        result = handle_show_slots({
            "specialist_id": str(massage_master.id),
            "service_id": str(massage_master.salon_service.id),
            "date": target.isoformat(),
        })

        assert result.action_type == ActionType.SHOW_SLOTS, result.action_data
        assert result.action_data["service"]["name"] == MASSAGE
        assert result.action_data["service"]["duration_minutes"] == 60

    def test_confirm_booking_resolves_canonical_service(self, massage_master):
        from ai.tools import ActionType
        from ai.tools_handlers import handle_confirm_booking

        _assert_pilot_shape()
        slot = timezone.now() + timedelta(days=1)

        result = handle_confirm_booking({
            "specialist_id": str(massage_master.id),
            "service_id": str(massage_master.salon_service.id),
            "datetime": slot.isoformat(),
        })

        assert result.action_type == ActionType.CONFIRM_BOOKING, result.action_data
        assert result.action_data["service_name"] == MASSAGE
        assert result.action_data["price"] == "2000.00"

    def test_unknown_service_still_asks_for_clarification(self, massage_master):
        """Тихого отката нет и в обратную сторону: чужой id — не слоты.

        Положительная стража рядом (`test_show_slots_...`) уже требует
        непустого разбора на тех же данных, поэтому этот тест не может
        пройти «потому что всё пусто».
        """
        import uuid

        from ai.tools import ActionType
        from ai.tools_handlers import handle_show_slots

        target = (timezone.localtime() + timedelta(days=1)).date()

        result = handle_show_slots({
            "specialist_id": str(massage_master.id),
            "service_id": str(uuid.uuid4()),
            "date": target.isoformat(),
        })

        assert result.action_type == ActionType.ASK_CLARIFICATION


# ---------------------------------------------------------------------------
# Смежная поверхность той же поломки — популярные категории на главной
# ---------------------------------------------------------------------------


class TestHomePopularCategories:
    def test_popular_categories_count_canonical_catalog(
        self, app_api, massage_master, manicure_master,
    ):
        """`_popular_categories` считает мастеров через `services__specialist`.

        Та же легаси-связь, что и в `layer_3_explore`: на пилоте у всех
        категорий счётчик 0, и полка «популярное» показывает нули.
        """
        _assert_pilot_shape()

        response = app_api.get(HOME_URL)
        assert response.status_code == 200, response.data
        rows = {
            row["name"]: row["specialists_count"]
            for row in response.data["data"]["popular_categories"]
        }

        assert rows.get("Массаж тела") == 1, rows
        assert rows.get("Маникюр") == 1, rows
