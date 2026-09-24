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

    def test_a_master_without_a_salon_is_not_demo(self, both_salons, client_person):
        """`SpecialistProfile.tenant` — `null=True`, и обычный
        `filter(tenant__is_demo=False)` дал бы INNER JOIN, молча выкосив
        каждого мастера без салона. Ровно этой ошибкой однажды уже сломали
        выдачу (DRF-1430), поэтому предикат обязан держать LEFT JOIN."""
        from users.sellable import demo_visibility_q, sellable_q

        lonely = _master(None, suffix="0003", display_name="Мастер Одиночка")
        lonely.tenant = None
        lonely.save(update_fields=["tenant"])

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
