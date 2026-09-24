"""Демо-салон остаётся в системе, но обычному клиенту не показывается (DRF-2420).

Демо-салоны сеялись за ТРЕМЯ замками (`seed_demo_salons`):
`Tenant.is_active=False`, `SpecialistProfile.status=PENDING`,
`is_booking_enabled=False`. Ключ `--activate` снимает все три разом — и это
главное про этот тикет: замки И БЫЛИ признаком демонстрационности, другого не
существовало. Их открыли, чтобы показывать интерфейс, и вместе с наглядностью
демо открылось обычному клиенту. Поэтому правильный ответ — отдельное
свойство, а не «закрыть замки обратно»: закрытые замки означают «салон
выключен», а нужно «салон живой, но не для клиента».

Признаков два, и оба — свойства, а не списки имён: `Tenant.is_demo` и
`User.is_test_persona`. Список слагов устареет на шестом салоне; слаги из файла
сида годятся ровно для разового проставления владельцем.

`is_proxy` за тестовость личности брать НЕЛЬЗЯ: он стоит у всех внешних
личностей, включая живых клиентов бота. Узел `TestProxyIsNotTestness` держит
это утверждение на месте.

Пять пулов, каждый — своим узлом в ОБЕ стороны. Одна сторона ничего не
доказывает: пустая выдача пройдёт и при работающем правиле, и при поломке по
другой причине, поэтому в каждом узле сначала утверждение о НАЛИЧИИ боевого
салона, и только потом об отсутствии демо.

* p1 — подбор, полки 1–2 (`users/recommendation_source.py`);
* p2 — полка 3, счётчики категорий (`users/catalog_recommendations_api.py`);
* p3 — движок главной Mini App (`ai/.../recommendation_engine.py`);
* p4 — глобальный поиск, все три выборки (`search/views.py`);
* p5 — публичный список и карточка мастера (`users/specialists_api.py`).

До этого тикета к таблице тенантов НЕ присоединялись p4 и p5 — там демо
удерживал единственный замок сида (`status`), и он открыт. Значит для них
правило появляется впервые, а не дублирует существующее.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from services.models import SalonService, ServiceCategory, ServiceTemplate, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile

User = get_user_model()

pytestmark = pytest.mark.django_db

REAL_NAME = "Мастер Боевой"
DEMO_NAME = "Мастер Демо"


# ---------------------------------------------------------------------------
# Данные: один боевой салон и один демонстрационный, оба ЖИВЫЕ
# ---------------------------------------------------------------------------


def _salon(*, slug: str, is_demo: bool) -> Tenant:
    return Tenant.objects.create(
        slug=slug, name=slug, city="Пенза", is_active=True, is_demo=is_demo,
    )


def _master(tenant: Tenant, *, suffix: str, display_name: str) -> SpecialistProfile:
    """Мастер, который продаётся: все три замка сида ОТКРЫТЫ.

    Именно это состояние и наступило на стенде после `--activate`, поэтому
    узлы говорят про демо, а не про выключенный салон.
    """
    user = User.objects.create_user(
        username=f"demo2420_{suffix}", password="x", role="specialist",
        phone=f"+7999242{suffix}",
    )
    # Профиль может быть уже создан сигналом на создание пользователя-мастера,
    # и тогда он DRAFT. Поля выставляются ПОСЛЕ get_or_create, иначе `defaults`
    # молча не применятся к существующей строке — узел это и поймал.
    profile, _ = SpecialistProfile.objects.get_or_create(
        user=user, defaults={"display_name": display_name, "bio": "t"},
    )
    profile.display_name = display_name
    profile.tenant = tenant
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.rating = 5
    profile.save(update_fields=[
        "display_name", "tenant", "status", "is_available",
        "is_booking_enabled", "rating",
    ])
    return profile


def _offer(profile: SpecialistProfile, *, name: str) -> SpecialistService:
    category, _ = ServiceCategory.objects.get_or_create(
        slug="demo2420-cat", defaults={"name": "Массаж"},
    )
    template, _ = ServiceTemplate.objects.get_or_create(
        name=f"{name} — {profile.pk}", category=category,
    )
    salon_service = SalonService.objects.create(
        tenant=profile.tenant, template=template, name=name,
        base_price=1000, duration_minutes=60, is_active=True,
    )
    return SpecialistService.objects.create(
        specialist=profile, salon_service=salon_service, tenant=profile.tenant,
        price=1000, duration_minutes=60, is_active=True,
    )


@pytest.fixture
def both_salons():
    real = _salon(slug="demo2420-real", is_demo=False)
    demo = _salon(slug="demo2420-demo", is_demo=True)
    real_master = _master(real, suffix="0001", display_name=REAL_NAME)
    demo_master = _master(demo, suffix="0002", display_name=DEMO_NAME)
    _offer(real_master, name="Массаж спины")
    _offer(demo_master, name="Массаж спины")
    return {"real": real_master, "demo": demo_master}


@pytest.fixture
def client_person():
    return User.objects.create_user(
        username="demo2420_client", password="x", role="client",
        phone="+79992420010", is_test_persona=False,
    )


@pytest.fixture
def test_person():
    """Личность для показа и съёмки эталонов: демо ВИДИТ (условие 2 тикета)."""
    return User.objects.create_user(
        username="demo2420_tester", password="x", role="client",
        phone="+79992420011", is_test_persona=True,
    )


# ---------------------------------------------------------------------------
# Предикат: одно определение видимости
# ---------------------------------------------------------------------------


class TestThePredicateItself:
    def test_a_client_sees_the_real_salon_and_not_the_demo(self, both_salons, client_person):
        from users.sellable import demo_visibility_q, sellable_q

        names = set(
            SpecialistProfile.objects
            .filter(sellable_q(), demo_visibility_q(client_person))
            .values_list("display_name", flat=True)
        )

        assert REAL_NAME in names
        assert DEMO_NAME not in names

    def test_a_test_persona_sees_both(self, both_salons, test_person):
        from users.sellable import demo_visibility_q, sellable_q

        names = set(
            SpecialistProfile.objects
            .filter(sellable_q(), demo_visibility_q(test_person))
            .values_list("display_name", flat=True)
        )

        assert {REAL_NAME, DEMO_NAME} <= names

    @pytest.mark.no_auto_tenant
    def test_a_master_without_a_salon_is_not_demo(self, both_salons, client_person):
        """`SpecialistProfile.tenant` — `null=True`, и обычный
        `filter(tenant__is_demo=False)` дал бы INNER JOIN, молча выкосив
        каждого мастера без салона. Ровно этой ошибкой однажды уже сломали
        выдачу (DRF-1430), поэтому предикат обязан держать LEFT JOIN.

        Маркер `no_auto_tenant` обязателен, и это не формальность: autouse
        фикстура `_auto_default_tenant` (conftest.py) на `pre_save`
        подставляет тенант любому профилю с `tenant_id=None`. Без маркера
        NULL в строке не окажется НИКОГДА, и узел зеленеет при любом
        предикате — в том числе при том самом голом
        `filter(tenant__is_demo=False)`, от которого он якобы защищает.
        Поэтому ниже сначала утверждается САМО УСЛОВИЕ опыта.
        """
        from users.sellable import demo_visibility_q, sellable_q

        lonely = _master(None, suffix="0003", display_name="Мастер Одиночка")
        lonely.tenant = None
        lonely.save(update_fields=["tenant"])
        lonely.refresh_from_db()
        assert lonely.tenant_id is None, (
            "условие опыта не выполнено: у мастера есть салон, значит про "
            "LEFT JOIN этот узел ничего не проверяет"
        )

        names = set(
            SpecialistProfile.objects
            .filter(sellable_q(), demo_visibility_q(client_person))
            .values_list("display_name", flat=True)
        )

        assert "Мастер Одиночка" in names
        assert DEMO_NAME not in names

    def test_an_unknown_viewer_is_treated_as_a_client(self, both_salons):
        """Нет личности — значит не тестовая: неизвестный смотрящий получает
        правило клиента, а не показ демо (fail-closed)."""
        from users.sellable import demo_visibility_q, sellable_q

        for viewer in (None, User(username="anon")):
            names = set(
                SpecialistProfile.objects
                .filter(sellable_q(), demo_visibility_q(viewer))
                .values_list("display_name", flat=True)
            )
            assert REAL_NAME in names
            assert DEMO_NAME not in names

    def test_the_prefix_form_works_for_offers(self, both_salons, client_person):
        """Поиск фильтрует услуги и связки через префикс `specialist` — тем же
        предикатом, что и профили."""
        from users.sellable import demo_visibility_q

        offers = set(
            SpecialistService.objects
            .filter(demo_visibility_q(client_person, "specialist"))
            .values_list("specialist__display_name", flat=True)
        )

        assert REAL_NAME in offers
        assert DEMO_NAME not in offers


class TestProxyIsNotTestness:
    def test_a_proxy_client_of_the_bot_does_not_see_demo(self, both_salons):
        """`is_proxy` стоит у ВСЕХ внешних личностей, включая живых клиентов
        бота. Взять его за тестовость значило бы показать демо всем — самая
        дорогая ошибка этого тикета."""
        from users.sellable import demo_visibility_q, sellable_q

        proxy_client = User.objects.create_user(
            username="bot:242001", password="x", role="client",
            phone="+79992420012", is_proxy=True,
        )

        names = set(
            SpecialistProfile.objects
            .filter(sellable_q(), demo_visibility_q(proxy_client))
            .values_list("display_name", flat=True)
        )

        assert REAL_NAME in names
        assert DEMO_NAME not in names


# ---------------------------------------------------------------------------
# Пять пулов, каждый в обе стороны (условие 1 тикета)
# ---------------------------------------------------------------------------


def _refs(facts) -> set[str]:
    return {str(f.ref.id) for f in facts}


class TestP1ThePickShelves:
    """Полки 1–2 подбора: `users/recommendation_source.py`."""

    def _facts(self, viewer):
        from recommendation._types import NeedOrigin, NeedSpec, Scope, ScopeMode
        from users.recommendation_source import SpecialistCandidateSource

        return SpecialistCandidateSource(viewer=viewer).fetch(
            scope=Scope(ScopeMode.MARKETPLACE),
            need=NeedSpec(origin=NeedOrigin.USER_EXPLICIT, raw_text="массаж"),
        )

    def test_a_client_gets_the_real_salon_only(self, both_salons, client_person):
        refs = _refs(self._facts(client_person))

        assert str(both_salons["real"].user_id) in refs
        assert str(both_salons["demo"].user_id) not in refs

    def test_a_test_persona_gets_both(self, both_salons, test_person):
        refs = _refs(self._facts(test_person))

        assert {str(both_salons["real"].user_id), str(both_salons["demo"].user_id)} <= refs


class TestP2TheCategoryCounters:
    """Полка 3, счётчики: `users/catalog_recommendations_api._catalog_pool`."""

    def _ids(self, viewer) -> set:
        from users.catalog_recommendations_api import _catalog_pool

        return set(
            _catalog_pool(goal="", goal_category_ids=None, viewer=viewer)
            .values_list("id", flat=True)
        )

    def test_a_client_counts_only_the_real_salon(self, both_salons, client_person):
        ids = self._ids(client_person)

        assert both_salons["real"].id in ids
        assert both_salons["demo"].id not in ids

    def test_a_test_persona_counts_both(self, both_salons, test_person):
        ids = self._ids(test_person)

        assert {both_salons["real"].id, both_salons["demo"].id} <= ids


class TestP3TheHomeEngine:
    """Движок главной Mini App: `ai/.../recommendation_engine.py`."""

    def _names(self, *, sees_demo: bool) -> set[str]:
        from ai.application.services.recommendation_engine import (
            RecommendationEngine,
            RecommendationQuery,
        )

        result = RecommendationEngine().recommend(
            RecommendationQuery(limit=20, viewer_sees_demo=sees_demo),
            use_cache=False,
        )
        return {c.display_name for c in result.candidates}

    def test_a_client_sees_the_real_salon_only(self, both_salons):
        names = self._names(sees_demo=False)

        assert REAL_NAME in names
        assert DEMO_NAME not in names

    def test_a_test_persona_sees_both(self, both_salons):
        names = self._names(sees_demo=True)

        assert {REAL_NAME, DEMO_NAME} <= names

    def test_the_two_audiences_do_not_share_a_cache_entry(self):
        """Ключ: иначе выдача тестовой личности досталась бы клиенту на весь
        TTL — особенно в анонимном пространстве ключей, где `client_id` пуст.

        Узел краснеет при выносе признака из digest — проверено подменой."""
        from ai.application.services.recommendation_engine import RecommendationQuery

        client_key = RecommendationQuery(limit=20).cache_key()
        tester_key = RecommendationQuery(limit=20, viewer_sees_demo=True).cache_key()

        assert client_key != tester_key

    def test_a_cached_persona_answer_is_not_served_to_a_client(self, both_salons):
        """И то же самое ЧЕРЕЗ КЭШ, а не только через ключ.

        Правило «демо видит только тестовая личность» отменяется не подбором, а
        кэшем: посчитанное для тестовой личности раздаётся настоящему клиенту
        весь срок жизни записи, и снаружи это выглядит нормальной выдачей.
        Поэтому узел ходит с включённым кэшем — тем самым путём, которым
        отмена и происходила бы.
        """
        from django.core.cache import cache

        from ai.application.services.recommendation_engine import (
            RecommendationEngine,
            RecommendationQuery,
        )

        cache.clear()
        engine = RecommendationEngine()

        persona = engine.recommend(RecommendationQuery(limit=20, viewer_sees_demo=True))
        # Сначала о НАЛИЧИИ: в прогретой записи демо действительно было, иначе
        # второе утверждение пройдёт на пустоте.
        assert DEMO_NAME in {c.display_name for c in persona.candidates}

        client = engine.recommend(RecommendationQuery(limit=20))

        names = {c.display_name for c in client.candidates}
        assert REAL_NAME in names
        assert DEMO_NAME not in names


class TestP4TheGlobalSearch:
    """Глобальный поиск, все три выборки: `search/views.py`.

    До этого тикета поиск к таблице салонов не присоединялся ВОВСЕ — демо
    удерживал единственный замок сида (`status`), и он открыт.
    """

    def _found(self, viewer) -> tuple[set[str], set[str]]:
        from rest_framework.test import APIClient

        client = APIClient()
        client.defaults["HTTP_X_APP_TYPE"] = "client"
        client.force_authenticate(user=viewer)
        response = client.get("/api/v1/search/", {"q": "Массаж", "limit": 50})
        assert response.status_code == 200, response.content
        body = response.json()
        payload = body.get("data", body)
        masters = {row["display_name"] for row in payload.get("specialists", [])}
        services = {
            row.get("specialist_name") or row.get("specialist", {}).get("display_name")
            for row in payload.get("services", [])
        }
        return masters, services

    def test_a_client_finds_the_real_salon_only(self, both_salons, client_person):
        masters, services = self._found(client_person)

        assert REAL_NAME in masters
        assert DEMO_NAME not in masters
        # Услуги — вторая и третья выборки той же ручки.
        assert REAL_NAME in services
        assert DEMO_NAME not in services

    def test_a_test_persona_finds_both(self, both_salons, test_person):
        masters, services = self._found(test_person)

        assert {REAL_NAME, DEMO_NAME} <= masters
        assert {REAL_NAME, DEMO_NAME} <= services


class TestP5ThePublicCatalog:
    """Публичный список и прямая карточка: `users/specialists_api.py`.

    Карточка проверяется отдельно от списка: правило, снимаемое прямой
    ссылкой, — не правило.
    """

    def _client(self, viewer):
        from rest_framework.test import APIClient

        client = APIClient()
        client.defaults["HTTP_X_APP_TYPE"] = "client"
        client.force_authenticate(user=viewer)
        return client

    def test_a_client_lists_the_real_salon_only(self, both_salons, client_person):
        response = self._client(client_person).get("/api/v1/specialists/", {"limit": 50})

        assert response.status_code == 200, response.content
        body = response.content.decode("utf-8")
        assert REAL_NAME in body
        assert DEMO_NAME not in body

    def test_a_test_persona_lists_both(self, both_salons, test_person):
        response = self._client(test_person).get("/api/v1/specialists/", {"limit": 50})

        assert response.status_code == 200, response.content
        body = response.content.decode("utf-8")
        assert REAL_NAME in body
        assert DEMO_NAME in body

    def test_a_direct_card_of_a_demo_master_is_closed_for_a_client(
        self, both_salons, client_person
    ):
        client = self._client(client_person)
        real = client.get(f"/api/v1/specialists/{both_salons['real'].id}/")
        demo = client.get(f"/api/v1/specialists/{both_salons['demo'].id}/")

        # Сначала о НАЛИЧИИ: боевая карточка открывается.
        assert real.status_code == 200, real.content
        assert demo.status_code == 404, demo.content

    def test_a_direct_card_of_a_demo_master_opens_for_a_test_persona(
        self, both_salons, test_person
    ):
        client = self._client(test_person)

        assert client.get(f"/api/v1/specialists/{both_salons['demo'].id}/").status_code == 200


# ---------------------------------------------------------------------------
# Утечки, которых не нашла моя перепись (найдены ревью)
# ---------------------------------------------------------------------------


class TestL1Favourites:
    """Избранное — живой путь по ПРЯМОМУ идентификатору.

    Идентификаторы демо-мастеров узнаваемы (салоны живые), а список избранного
    рисует полную карточку: адрес салона, цены услуг. Мой же критерий из
    докстринга P5 — «правило, снимаемое прямой ссылкой, — не правило» —
    сработал против меня: перепись пяти пулов избранное не нашла.
    """

    def _client(self, viewer):
        from rest_framework.test import APIClient

        client = APIClient()
        client.defaults["HTTP_X_APP_TYPE"] = "client"
        client.force_authenticate(user=viewer)
        return client

    def test_a_client_cannot_favourite_a_demo_master(self, both_salons, client_person):
        client = self._client(client_person)

        real = client.post(f"/api/v1/favorites/specialists/{both_salons['real'].id}/")
        demo = client.post(f"/api/v1/favorites/specialists/{both_salons['demo'].id}/")

        # Сначала о НАЛИЧИИ: боевого мастера в избранное добавить можно.
        assert real.status_code in (200, 201), real.content
        assert demo.status_code == 404, demo.content

    def test_a_demo_master_already_favourited_disappears_from_the_list(
        self, both_salons, client_person
    ):
        """Строка могла быть создана ДО правки — тогда карточка не должна
        рисоваться, хотя запись в избранном осталась."""
        from users.models import FavoriteSpecialist

        FavoriteSpecialist.objects.create(
            user=client_person, specialist=both_salons["demo"]
        )
        FavoriteSpecialist.objects.create(
            user=client_person, specialist=both_salons["real"]
        )

        response = self._client(client_person).get("/api/v1/favorites/specialists/")

        assert response.status_code == 200, response.content
        body = response.content.decode("utf-8")
        assert REAL_NAME in body
        assert DEMO_NAME not in body

    def test_a_test_persona_keeps_seeing_both(self, both_salons, test_person):
        from users.models import FavoriteSpecialist

        for profile in (both_salons["real"], both_salons["demo"]):
            FavoriteSpecialist.objects.create(user=test_person, specialist=profile)

        response = self._client(test_person).get("/api/v1/favorites/specialists/")

        body = response.content.decode("utf-8")
        assert REAL_NAME in body
        assert DEMO_NAME in body


class TestL5TheConcierge:
    """Чат Ayla: правка едва не отняла у владельца показ чата.

    До ревью контекст консьержа строился без признака, то есть после правки
    демо не видела и ТЕСТОВАЯ личность — а показ чата и есть причина, по
    которой демо-салоны держат живыми.
    """

    def _names(self, actor) -> set[str]:
        from ai.concierge_factory import build_specialist_context_for_actor

        context = build_specialist_context_for_actor(actor)
        return {c.display_name for c in context.candidates}

    def test_a_client_gets_the_real_salon_only(self, both_salons, client_person):
        names = self._names(client_person)

        assert REAL_NAME in names
        assert DEMO_NAME not in names

    def test_a_test_persona_gets_both(self, both_salons, test_person):
        names = self._names(test_person)

        assert {REAL_NAME, DEMO_NAME} <= names
