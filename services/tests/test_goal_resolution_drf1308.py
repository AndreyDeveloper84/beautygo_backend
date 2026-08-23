"""DRF-1308 — цель доезжает от корневой категории до услуги и до бота.

Фикстуры воспроизводят форму пилотного контура на 23.08, а не удобную
форму: цель курируется на КОРНЕ, услуга висит на ЛИСТЕ, а у пакетной
услуги собственная категория бесцелевая и цель выводима только через
канонический шаблон. До этой задачи такой каталог давал 0 услуг с целью
на любом конце.

Проверяется:
- «цель → категории»: корень раскрывается вниз до подкатегорий
  (`goals.resolution.resolve_goal_category_ids`);
- «услуга → цели»: подъём к ближайшему предку со связью (п. 1 решения
  владельца) и фолбэк на категорию шаблона;
- отсутствие ложной цели там, где владелец её не заявлял (п. 4);
- поле `goals` в ответе внутреннего каталога и его форма.
"""
from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from goals.models import ClientGoal
from goals.resolution import resolve_goal_category_ids
from services.goal_resolution import (
    build_category_goal_index,
    expand_categories_with_descendants,
)
from services.models import (
    GoalOption,
    GoalOptionCategory,
    SalonService,
    ServiceCategory,
    ServiceTemplate,
)
from tenants.models import Tenant
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-drf1308"
SALON_URL = "/api/v1/internal/catalog/salon-services/"


# ---------------------------------------------------------------------------
# Фикстуры: форма пилота — цели на корнях, услуги на листьях
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="drf1308-t", name="DRF-1308 Tenant")


@pytest.fixture
def massage_root(db):
    """Корень «Массаж тела» — сюда владелец вешает цель."""
    return ServiceCategory.objects.create(name="Массаж тела", slug="massage-body")


@pytest.fixture
def massage_leaf(massage_root):
    """Лист «Базовый ручной массаж» — сюда цепляются услуги салона."""
    return ServiceCategory.objects.create(
        name="Базовый ручной массаж", slug="massage-manual", parent=massage_root,
    )


@pytest.fixture
def laser_root(db):
    return ServiceCategory.objects.create(
        name="Лазерная эпиляция и удаление волос", slug="laser-root",
    )


@pytest.fixture
def laser_leaf(laser_root):
    return ServiceCategory.objects.create(
        name="Лазерная эпиляция", slug="laser-leaf", parent=laser_root,
    )


@pytest.fixture
def packages_root(db):
    """Коммерческий контейнер салона — цели у него нет и не будет (п. 4)."""
    return ServiceCategory.objects.create(
        name="Комплексные программы и пакеты", slug="packages-root",
    )


@pytest.fixture
def relax_goal(massage_root):
    option = GoalOption.objects.create(
        key="relax", label="Расслабиться и снять стресс", sort_order=10,
    )
    GoalOptionCategory.objects.create(goal_option=option, category=massage_root)
    return option


@pytest.fixture
def self_care_goal(laser_root):
    """Пункт 2 решения владельца: лазерная эпиляция → «Привести себя в порядок»."""
    option = GoalOption.objects.create(
        key="self_care", label="Привести себя в порядок", sort_order=20,
    )
    GoalOptionCategory.objects.create(goal_option=option, category=laser_root)
    return option


@pytest.fixture
def leaf_service(tenant, massage_leaf):
    """Обычная услуга: своя категория — лист под целевым корнем."""
    return SalonService.objects.create(
        tenant=tenant, category=massage_leaf, name="Спина и шея, 30 мин",
    )


@pytest.fixture
def package_service(tenant, packages_root, laser_leaf):
    """Пакет «Афродиты»: своя категория бесцелевая, шаблон — канонический.

    Ровно тот случай, который на контуре закрывается только фолбэком на
    `template.category`: собственная категория салона это витрина, а не
    предметная ветка.
    """
    template = ServiceTemplate.objects.create(
        category=laser_leaf, name="Комплекс «подмышки + бикини»",
        name_short="Комплекс", duration_default=45,
    )
    return SalonService.objects.create(
        tenant=tenant, template=template, category=packages_root,
        name="Комплекс «подмышки + бикини»",
    )


@pytest.fixture
def orphan_package_service(tenant, packages_root):
    """Пакет пилота без шаблона: цели нет ни по дереву, ни по канону."""
    return SalonService.objects.create(
        tenant=tenant, category=packages_root,
        name="Спина без боли — комплекс массажа",
    )


def _api(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    client = APIClient()
    if bearer is not None:
        client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return client


# ---------------------------------------------------------------------------
# Направление «цель → категории»
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGoalToCategories:
    def test_root_binding_reaches_services_on_leaves(
        self, relax_goal, massage_root, massage_leaf, leaf_service,
    ):
        """Регрессия ядра задачи: до фикса выдача была пустой.

        Связь цели ведёт на корень, услуга висит на листе — и без
        раскрытия дерева фильтр по `category_id` не находил ничего.
        """
        user = User.objects.create_user(
            username="drf1308-a", password="x", role="client",
            phone="+79995001301", is_proxy=True,
        )
        ClientGoal.objects.create(client=user, goal_key="relax", source_channel="bot")

        resolved = resolve_goal_category_ids(user)

        assert resolved == [massage_root.id, massage_leaf.id]
        assert SalonService.objects.filter(category_id__in=resolved).count() == 1

    def test_expansion_keeps_root_first_and_deduplicates(
        self, massage_root, massage_leaf,
    ):
        expanded = expand_categories_with_descendants(
            [massage_root.id, massage_leaf.id, massage_root.id],
        )
        assert expanded == [massage_root.id, massage_leaf.id]

    def test_inactive_subcategory_is_not_exposed_through_goal(
        self, massage_root, massage_leaf,
    ):
        """Скрытая ветка каталога не должна возвращаться через цель."""
        massage_leaf.is_active = False
        massage_leaf.save()
        assert expand_categories_with_descendants([massage_root.id]) == [
            massage_root.id,
        ]

    def test_unbound_goal_still_resolves_to_nothing(self, db):
        """Пустая связь остаётся пустой: раскрытие ничего не выдумывает."""
        GoalOption.objects.create(key="event", label="Собраться к событию")
        user = User.objects.create_user(
            username="drf1308-b", password="x", role="client",
            phone="+79995001302", is_proxy=True,
        )
        ClientGoal.objects.create(client=user, goal_key="event", source_channel="bot")
        assert resolve_goal_category_ids(user) is None


# ---------------------------------------------------------------------------
# Направление «услуга → цели»
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestServiceToGoals:
    def test_leaf_service_inherits_goal_from_root(self, relax_goal, leaf_service):
        """Пункт 1 решения владельца: подъём к ближайшему предку со связью."""
        index = build_category_goal_index()
        assert index.goals_for_service(leaf_service) == [
            {"key": "relax", "label": "Расслабиться и снять стресс"},
        ]

    def test_package_falls_back_to_template_category(
        self, self_care_goal, package_service,
    ):
        """Своя категория бесцелевая — цель берётся из категории шаблона."""
        index = build_category_goal_index()
        assert index.goals_for_service(package_service) == [
            {"key": "self_care", "label": "Привести себя в порядок"},
        ]

    def test_own_binding_wins_over_template(
        self, relax_goal, self_care_goal, tenant, massage_leaf, laser_leaf,
    ):
        """Фолбэк, а не объединение: своя цепочка побеждает.

        Салон может ошибиться с категорией; тогда объединение приписало бы
        услуге чужую цель. Fallback этого не делает.
        """
        template = ServiceTemplate.objects.create(
            category=laser_leaf, name="Шаблон из другой ветки",
            name_short="Шаблон", duration_default=30,
        )
        service = SalonService.objects.create(
            tenant=tenant, template=template, category=massage_leaf,
            name="Услуга с чужим шаблоном",
        )
        index = build_category_goal_index()
        assert index.goals_for_service(service) == [
            {"key": "relax", "label": "Расслабиться и снять стресс"},
        ]

    def test_package_without_binding_or_template_stays_empty(
        self, relax_goal, self_care_goal, orphan_package_service,
    ):
        """Пункт 4 решения владельца: ложной цели ради покрытия не создаём."""
        index = build_category_goal_index()
        assert index.goals_for_service(orphan_package_service) == []

    def test_inactive_goal_option_is_not_exposed(self, relax_goal, leaf_service):
        relax_goal.is_active = False
        relax_goal.save()
        index = build_category_goal_index()
        assert index.goals_for_service(leaf_service) == []

    def test_goals_ordered_by_option_sort_order(
        self, massage_root, massage_leaf, leaf_service,
    ):
        second = GoalOption.objects.create(
            key="recharge", label="Восстановить силы", sort_order=5,
        )
        first = GoalOption.objects.create(
            key="relax", label="Расслабиться и снять стресс", sort_order=10,
        )
        GoalOptionCategory.objects.create(goal_option=first, category=massage_root)
        GoalOptionCategory.objects.create(goal_option=second, category=massage_root)

        index = build_category_goal_index()
        assert [g["key"] for g in index.goals_for_service(leaf_service)] == [
            "recharge", "relax",
        ]


# ---------------------------------------------------------------------------
# Второй разрыв: цели доезжают до зеркала бота
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestGoalsOnCatalogMirror:
    def test_payload_carries_resolved_goals(self, relax_goal, leaf_service):
        response = _api().get(f"{SALON_URL}{leaf_service.id}/")
        assert response.status_code == 200, response.data
        assert response.json()["goals"] == [
            {"key": "relax", "label": "Расслабиться и снять стресс"},
        ]

    def test_payload_goals_empty_when_unresolvable(
        self, relax_goal, orphan_package_service,
    ):
        response = _api().get(f"{SALON_URL}{orphan_package_service.id}/")
        assert response.status_code == 200, response.data
        assert response.json()["goals"] == []

    def test_query_count_does_not_grow_with_catalog_size(
        self, relax_goal, self_care_goal, tenant, massage_leaf, leaf_service,
    ):
        """Индекс собирается один раз на запрос, а не по строке (N+1).

        Проверяется инвариант, а не литерал: сравниваются два одинаковых
        запроса к каталогу из 1 и из 21 услуги. Литеральное число
        запросов ломалось бы на любой посторонней правке вьюсета и
        провоцировало бы подгонку assert под поведение.
        """
        with CaptureQueriesContext(connection) as small:
            assert _api().get(SALON_URL).status_code == 200

        SalonService.objects.bulk_create([
            SalonService(
                tenant=tenant, category=massage_leaf, name=f"Массаж {i}",
            )
            for i in range(20)
        ])

        with CaptureQueriesContext(connection) as large:
            response = _api().get(SALON_URL)
        assert response.status_code == 200

        assert len(large) == len(small), (
            "число запросов выросло вместе с каталогом — вернулся N+1"
        )
