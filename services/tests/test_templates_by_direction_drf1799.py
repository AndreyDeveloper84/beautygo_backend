"""Шаблоны направления одним запросом — ``?direction_id=`` (DRF-1799, M7a).

``GET /api/v1/internal/services/templates/?category_id=`` отбирает шаблоны
строго по одной категории, а канон вешает шаблон на подкатегорию
(``seed_canonical_catalog``: «category = subcategory row if present, else
root»). По id корня направления этот запрос возвращал только то, что висит
прямо на корне, — экран 03 «по направлению» был бы почти пустым.

Что заперто:

- ``direction_id`` отдаёт всё поддерево корня на любой глубине (в фикстуре
  глубина 3 — не держится на depth ≤ 2), чужое направление — нет; у каждого
  шаблона — ``category_id`` / ``category_name``;
- направление — тот же корень, что у ``/directions/`` (без родителя, без
  тенанта, активный): подкатегория и салонный корень — 400 ``NOT_A_DIRECTION``,
  неизвестный id — 404, не UUID — 400;
- ``category_id`` и ``direction_id`` вместе — 400
  ``CATEGORY_AND_DIRECTION_EXCLUSIVE``, а не молча один из них;
- режим ``category_id`` не изменился;
- определение направления одно: выбор услуг (DRF-1912) и этот запрос берут
  обход дерева из ``services.taxonomy``.
"""
from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from services.models import ServiceCategory, ServiceTemplate
from tenants.models import Tenant

pytestmark = pytest.mark.django_db

TOKEN = "test-internal-token-1799"  # noqa: S105
URL = "/api/v1/internal/services/templates/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


def _api(bearer: str | None = TOKEN) -> APIClient:
    api = APIClient()
    if bearer is not None:
        api.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return api


def _category(name, parent=None, tenant=None, sort_order=0):
    return ServiceCategory.objects.create(
        name=name, slug=f"c1799-{uuid.uuid4().hex[:10]}", parent=parent, tenant=tenant,
        sort_order=sort_order, is_active=True,
    )


def _attach(child, parent):
    """Третий уровень — только мимо ``save()``: ``ServiceCategory.clean()`` запрещает
    глубину > 2, а в базе ограничения нет, и такие строки появляются через
    ``update``, bulk и старые данные (тот же приём, что в DRF-1912). Поддерево
    обязано их выдержать, иначе сторож держался бы на глубине ≤ 2."""
    ServiceCategory.objects.filter(pk=child.pk).update(parent=parent)
    child.refresh_from_db()


def _template(category, name):
    return ServiceTemplate.objects.create(category=category, name=name, name_short=name[:40])


@pytest.fixture
def tree(db):
    root = _category("1799 Ногти", sort_order=1)
    sub = _category("1799 Маникюр", parent=root, sort_order=1)
    subsub = _category("1799 Аппаратный", sort_order=1)
    _attach(subsub, sub)  # глубина 3
    other_root = _category("1799 Брови", sort_order=2)
    salon = Tenant.objects.create(slug="tax1799", name="Taxonomy Salon")
    salon_root = _category("1799 Салонное", tenant=salon, sort_order=3)
    return {
        "root": root, "sub": sub, "subsub": subsub,
        "other_root": other_root, "salon_root": salon_root,
        "on_root": _template(root, "1799 Покрытие гель-лак"),
        "on_sub": _template(sub, "1799 Классический маникюр"),
        "on_subsub": _template(subsub, "1799 Аппаратный маникюр"),
        "on_other": _template(other_root, "1799 Коррекция бровей"),
        "on_salon": _template(salon_root, "1799 Салонная услуга"),
    }


def _get(**params):
    return _api().get(URL, params)


def _names(resp) -> set[str]:
    return {t["name"] for t in resp.json()["data"]["templates"]}


class TestWholeSubtree:
    def test_a_direction_returns_templates_at_every_depth(self, tree):
        resp = _get(direction_id=tree["root"].id, region="default")
        assert resp.status_code == 200, resp.content
        assert _names(resp) == {
            "1799 Покрытие гель-лак", "1799 Классический маникюр", "1799 Аппаратный маникюр",
        }

    def test_the_depth_three_template_is_there(self, tree):
        resp = _get(direction_id=tree["root"].id, region="default")
        assert "1799 Аппаратный маникюр" in _names(resp)

    def test_another_direction_is_not_mixed_in(self, tree):
        names = _names(_get(direction_id=tree["root"].id, region="default"))
        assert "1799 Покрытие гель-лак" in names
        assert "1799 Коррекция бровей" not in names and "1799 Салонная услуга" not in names

    def test_each_template_names_its_category(self, tree):
        rows = {t["name"]: t for t in _get(direction_id=tree["root"].id, region="default").json()["data"]["templates"]}
        deep = rows["1799 Аппаратный маникюр"]
        assert deep["category_id"] == str(tree["subsub"].id)
        assert deep["category_name"] == "1799 Аппаратный"
        assert rows["1799 Покрытие гель-лак"]["category_name"] == "1799 Ногти"


class TestRefusals:
    def test_category_and_direction_together_is_refused_by_name(self, tree):
        resp = _get(direction_id=tree["root"].id, category_id=tree["sub"].id)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "CATEGORY_AND_DIRECTION_EXCLUSIVE"

    def test_a_subcategory_is_not_a_direction(self, tree):
        resp = _get(direction_id=tree["sub"].id)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "NOT_A_DIRECTION"

    def test_a_salon_root_is_not_a_direction(self, tree):
        resp = _get(direction_id=tree["salon_root"].id)
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "NOT_A_DIRECTION"

    def test_unknown_direction_is_404(self, tree):
        assert _get(direction_id=uuid.uuid4()).status_code == 404

    def test_not_a_uuid_is_400(self, tree):
        assert _get(direction_id="not-a-uuid").status_code == 400

    def test_bearer_is_required(self, tree):
        assert _api(bearer=None).get(URL, {"direction_id": tree["root"].id}).status_code == 403


class TestCategoryModeUnchanged:
    def test_category_id_still_means_exactly_that_category(self, tree):
        resp = _get(category_id=tree["sub"].id, region="default")
        assert resp.status_code == 200, resp.content
        rows = resp.json()["data"]["templates"]
        assert [t["name"] for t in rows] == ["1799 Классический маникюр"]
        assert "category_id" not in rows[0]


class TestOneDefinition:
    def test_selection_and_templates_share_the_tree_walk(self):
        from services import internal_offer_api, taxonomy, templates_views

        assert internal_offer_api.category_roots is taxonomy.category_roots
        assert templates_views.direction_category_ids is taxonomy.direction_category_ids
        assert not hasattr(internal_offer_api, "_category_roots")
