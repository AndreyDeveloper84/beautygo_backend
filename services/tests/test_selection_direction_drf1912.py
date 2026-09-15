"""DRF-1912: направление (корень канона шаблона) в ответе selection.

``GET /api/v1/internal/specialists/{id}/services/selection/`` (M8a) — у каждой
строки теперь есть группа для экрана 04 «по направлениям» (DRF-1810):

* ``direction_id`` / ``direction_name`` / ``direction_sort_order`` — САМЫЙ
  ВЕРХНИЙ предок категории ШАБЛОНА (канон первичен), на любой глубине;
* ``category_name`` — категория шаблона (подкатегория, если она есть);
* категория строки салона источником группы не является: модератор может её
  сменить, группа остаётся от шаблона;
* число запросов не растёт ни с числом строк, ни с глубиной дерева;
* цикл в дереве категорий не вешает ответ.

Оговорка решения: корни канона (22) ≠ 6 направлений экрана 02 — открытый
вопрос владельцу G7; до решения группа = корень канона.

Красный до правки: все, кроме сторожа числа запросов — полей нет (KeyError).
Сторож числа запросов зелёный и до правки (лишнего запроса ещё нет); что он
кусается, доказывает проба «запрос предков на строку».
"""

from __future__ import annotations

import uuid

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from services.models import SalonService, ServiceCategory, ServiceTemplate
from tenants.solo_provisioning import provision_solo_workspace

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1912"  # noqa: S105
OLGA = "bot:max:1912001"

M8A_ITEM_KEYS = {
    "salon_service_id",
    "template_id",
    "name",
    "category_id",
    "is_active",
    "mapping_status",
    "offer",
    "configured",
}
DIRECTION_KEYS = {"direction_id", "direction_name", "direction_sort_order", "category_name"}


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN


@pytest.fixture
def olga():
    return provision_solo_workspace(
        tenant_id=uuid.uuid4(),
        slug="solo-max-1912olga",
        name="Студия 1912",
        city="Пенза",
        external_user_id=OLGA,
        display_name="Мастер",
    ).profile


def _category(name: str, *, parent: ServiceCategory | None = None, sort_order: int = 0):
    suffix = uuid.uuid4().hex[:8]
    return ServiceCategory.objects.create(
        name=f"{name} {suffix}", slug=f"c1912-{suffix}", parent=parent, sort_order=sort_order
    )


def _attach(child: ServiceCategory, parent: ServiceCategory) -> None:
    """Третий уровень — только так: ``ServiceCategory.clean()`` запрещает глубину > 2
    на ``save()`` (services/models.py:38-51), а в базе ограничения нет. Такие данные
    появляются мимо ``save()`` (``update``, bulk, старые строки) — проход до корня
    обязан их выдержать, иначе «родитель» выглядел бы «корнем» на глубине 2."""
    ServiceCategory.objects.filter(pk=child.pk).update(parent=parent)
    child.refresh_from_db()


def _template(category: ServiceCategory, name: str) -> ServiceTemplate:
    return ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:40], duration_default=60
    )


def _client() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = OLGA
    return c


def _url(profile) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/services/selection/"


def _select(profile, *templates) -> None:
    resp = _client().post(_url(profile), {"template_ids": [str(t.pk) for t in templates]}, format="json")
    assert resp.status_code in (200, 201), resp.content


def _data(resp) -> dict:
    body = resp.json()
    return body.get("data", body)


def _item(profile, template: ServiceTemplate) -> dict:
    resp = _client().get(_url(profile))
    assert resp.status_code == 200, resp.content
    rows = {row["template_id"]: row for row in _data(resp)["services"]}
    return rows[str(template.pk)]


class TestDirectionIsTheRootOfTheTemplatesCategory:
    def test_depth_1_template_directly_under_a_root(self, olga):
        root = _category("Брови", sort_order=7)
        tpl = _template(root, "Коррекция 1912 d1")
        _select(olga, tpl)

        item = _item(olga, tpl)

        assert item["direction_id"] == str(root.pk)
        assert item["direction_name"] == root.name
        assert item["direction_sort_order"] == 7
        assert item["category_name"] == root.name

    def test_depth_2_seed_shape(self, olga):
        root = _category("Ногти", sort_order=3)
        sub = _category("Маникюр", parent=root, sort_order=1)
        tpl = _template(sub, "Маникюр классический 1912 d2")
        _select(olga, tpl)

        item = _item(olga, tpl)

        assert item["direction_id"] == str(root.pk)
        assert item["direction_name"] == root.name
        assert item["direction_sort_order"] == 3
        assert item["category_name"] == sub.name

    def test_depth_3_top_most_root_not_the_parent(self, olga):
        root = _category("Волосы", sort_order=5)
        mid = _category("Окрашивание", parent=root, sort_order=2)
        leaf = _category("Сложное окрашивание", sort_order=9)
        _attach(leaf, mid)  # данные, минувшие save()
        tpl = _template(leaf, "Шатуш 1912 d3")
        _select(olga, tpl)

        item = _item(olga, tpl)

        assert item["direction_id"] == str(root.pk)
        assert item["direction_name"] == root.name
        assert item["direction_sort_order"] == 5
        assert item["category_name"] == leaf.name


class TestTheCanonIsTheSource:
    def test_a_moderator_changing_the_rows_category_keeps_the_templates_direction(self, olga):
        root_a = _category("Лицо", sort_order=1)
        sub_a = _category("Чистка", parent=root_a)
        root_b = _category("Тело", sort_order=2)
        tpl = _template(sub_a, "Чистка лица 1912")
        _select(olga, tpl)
        # Модератор/админ переносит строку салона в другую категорию.
        SalonService.objects.filter(tenant_id=olga.tenant_id, template=tpl).update(category=root_b)

        item = _item(olga, tpl)

        assert item["category_id"] == str(root_b.pk)  # поле строки — как было, M8a не меняется
        assert item["direction_id"] == str(root_a.pk)
        assert item["category_name"] == sub_a.name


class TestShape:
    def test_m8a_fields_stay_and_the_four_direction_fields_are_added(self, olga):
        root = _category("Ресницы", sort_order=4)
        sub = _category("Наращивание", parent=root)
        tpl = _template(sub, "Наращивание 1912 shape")
        _select(olga, tpl)

        item = _item(olga, tpl)

        assert M8A_ITEM_KEYS | DIRECTION_KEYS <= set(item)
        assert isinstance(item["direction_id"], str)
        assert isinstance(item["direction_name"], str)
        assert isinstance(item["direction_sort_order"], int)
        assert isinstance(item["category_name"], str)


class TestNoQueryPerRowOrLevel:
    def test_query_count_does_not_grow_with_rows_or_depth(self, olga):
        first_root = _category("Массаж", sort_order=1)
        _select(olga, _template(first_root, "Массаж 1912 q1"))
        with CaptureQueriesContext(connection) as one_row:
            assert _client().get(_url(olga)).status_code == 200

        roots = [_category(f"Корень {i}", sort_order=10 + i) for i in range(2)]
        mid = _category("Середина", parent=roots[0])
        leaf = _category("Лист")
        _attach(leaf, mid)  # данные, минувшие save()
        more = [
            _template(roots[0], "Шаблон 1912 q2"),
            _template(mid, "Шаблон 1912 q3"),
            _template(leaf, "Шаблон 1912 q4"),
            _template(roots[1], "Шаблон 1912 q5"),
        ]
        _select(olga, *more)
        with CaptureQueriesContext(connection) as five_rows:
            resp = _client().get(_url(olga))
        assert resp.status_code == 200
        assert len(_data(resp)["services"]) == 5

        assert len(five_rows.captured_queries) == len(one_row.captured_queries)


class TestCycleGuard:
    def test_a_cycle_in_the_category_tree_does_not_hang(self, olga):
        a = _category("Цикл A")
        b = _category("Цикл B", parent=a)
        ServiceCategory.objects.filter(pk=a.pk).update(parent=b)  # a → b → a
        tpl = _template(a, "Шаблон в цикле 1912")
        _select(olga, tpl)

        item = _item(olga, tpl)

        assert item["direction_id"] in {str(a.pk), str(b.pk)}
        assert item["category_name"] == a.name
