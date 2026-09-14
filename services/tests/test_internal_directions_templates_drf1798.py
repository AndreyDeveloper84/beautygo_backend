"""DRF-1798 (M6 карты онбординга мастера) — направления и канонические шаблоны под внутренним Bearer.

Экран «Чем вы занимаетесь?» и экран выбора услуг Mini App ходят через
бота; у бота нет JWT Pro-приложения. Два маршрута чтения:

* ``GET /api/v1/internal/services/directions/`` — корни глобальной
  таксономии (``parent IS NULL``, ``tenant IS NULL``, ``is_active``),
  без счётчиков мастеров — число, которое некому доказать, не отдаётся;
* ``GET /api/v1/internal/services/templates/?category_id=`` — ровно тот
  же payload, что у Pro-маршрута ``/api/v1/service-templates/`` (один
  ``_build_payload``), под ``IsInternalBearer``.

Шесть названий макета в код не зашиты: рантайм читает каталог, а
расхождение макета с каноном записано в карте отдельно.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from services.models import ServiceCategory, ServiceTemplate
from tenants.models import Tenant
from users.models import User

VALID_TOKEN = "test-ayla-internal-token-1798"
DIRECTIONS_URL = "/api/v1/internal/services/directions/"
TEMPLATES_URL = "/api/v1/internal/services/templates/"
PRO_TEMPLATES_URL = "/api/v1/service-templates/"
INTERNAL_CATEGORIES_URL = "/api/v1/internal/services/categories/"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def salon(db) -> Tenant:
    return Tenant.objects.create(slug="t1798", name="Салон 1798")


@pytest.fixture
def canon_roots(db) -> dict[str, ServiceCategory]:
    """Три корня канона + один неактивный корень + один салонный корень + подкатегория."""
    manicure = ServiceCategory.objects.create(name="1798 Маникюр", slug="c1798-manicure", sort_order=2)
    massage = ServiceCategory.objects.create(name="1798 Массаж", slug="c1798-massage", sort_order=1)
    brows = ServiceCategory.objects.create(name="1798 Брови", slug="c1798-brows", sort_order=3)
    ServiceCategory.objects.create(
        name="1798 Снятый корень", slug="c1798-off", sort_order=0, is_active=False,
    )
    ServiceCategory.objects.create(
        name="1798 Гель-лак", slug="c1798-gel", parent=manicure, sort_order=0,
    )
    return {"manicure": manicure, "massage": massage, "brows": brows}


@pytest.fixture
def salon_root(salon) -> ServiceCategory:
    return ServiceCategory.objects.create(
        name="1798 Салонный корень", slug="c1798-salon", tenant=salon, sort_order=0,
    )


def _api(*, bearer: str | None = VALID_TOKEN) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


def _pro() -> APIClient:
    user = User.objects.create_user(
        username="pro1798", role="specialist", phone="+79995317980", is_verified=True,
    )
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "pro"
    c.force_authenticate(user=user)
    return c


class TestDirectionsAuthBoundary:
    def test_missing_bearer_denied(self, canon_roots):
        assert _api(bearer=None).get(DIRECTIONS_URL).status_code == 403

    def test_wrong_bearer_denied(self, canon_roots):
        assert _api(bearer="nope").get(DIRECTIONS_URL).status_code == 403

    def test_empty_token_fails_closed(self, settings, canon_roots):
        settings.AYLA_INTERNAL_API_TOKEN = ""
        assert _api().get(DIRECTIONS_URL).status_code == 403


class TestDirectionsAreTheCanonRoots:
    def test_only_active_global_roots_in_sort_order(self, canon_roots, salon_root):
        r = _api().get(DIRECTIONS_URL)
        assert r.status_code == 200, r.content
        names = [row["name"] for row in r.json()["data"]]

        # Положительная половина: три активных глобальных корня, в порядке sort_order.
        assert names == ["1798 Массаж", "1798 Маникюр", "1798 Брови"]
        # Отрицательная половина на тех же данных: снятый корень, подкатегория
        # и салонный корень — не направления.
        assert "1798 Снятый корень" not in names
        assert "1798 Гель-лак" not in names
        assert "1798 Салонный корень" not in names

    def test_the_salon_root_is_excluded_by_tenant_not_by_accident(self, canon_roots, salon_root):
        """Соседний внутренний маршрут категорий салонный корень ОТДАЁТ — значит,
        исключает его именно фильтр направлений, а не сломанная фикстура."""
        neighbour = _api().get(INTERNAL_CATEGORIES_URL)
        assert neighbour.status_code == 200, neighbour.content
        body = neighbour.json()
        # Соседний маршрут пагинирован DRF-ом: {"count", "results": [...]}.
        rows = body["results"] if isinstance(body, dict) and "results" in body else body
        assert "1798 Салонный корень" in {row["name"] for row in rows}

        internal = _api().get(DIRECTIONS_URL).json()["data"]
        assert "1798 Салонный корень" not in {row["name"] for row in internal}

    def test_no_invented_numbers(self, canon_roots):
        """Счётчик мастеров считает legacy-слой (на пилоте ноль у каждого) —
        экрану направлений он не отдаётся вовсе."""
        rows = _api().get(DIRECTIONS_URL).json()["data"]
        assert rows, "positive control: направления есть"
        for row in rows:
            assert set(row) == {"id", "name", "slug", "icon", "sort_order"}
            assert "specialists_count" not in row
            assert "children" not in row


class TestTemplatesUnderInternalBearer:
    @pytest.fixture
    def templates(self, canon_roots) -> ServiceCategory:
        cat = canon_roots["manicure"]
        ServiceTemplate.objects.create(
            category=cat, name="1798 Аппаратный маникюр", name_short="Аппаратный",
            duration_default=60, lifecycle=ServiceTemplate.Lifecycle.APPROVED, sort_order=1,
            approved_at=timezone.now(), approval_source_ref="test-1798",
            approved_rule="test-1798", approval_rule_version="1",
        )
        ServiceTemplate.objects.create(
            category=cat, name="1798 Японский маникюр", name_short="Японский",
            duration_default=90, lifecycle=ServiceTemplate.Lifecycle.PROVISIONAL, sort_order=2,
        )
        return cat

    def test_missing_bearer_denied(self, templates):
        assert _api(bearer=None).get(f"{TEMPLATES_URL}?category_id={templates.id}").status_code == 403

    def test_wrong_bearer_denied(self, templates):
        assert _api(bearer="nope").get(f"{TEMPLATES_URL}?category_id={templates.id}").status_code == 403

    def test_same_payload_as_the_pro_route(self, templates):
        """Один ``_build_payload`` — тела совпадают байт в байт на одном регионе."""
        internal = _api().get(f"{TEMPLATES_URL}?category_id={templates.id}&region=default")
        pro = _pro().get(f"{PRO_TEMPLATES_URL}?category_id={templates.id}&region=default")
        assert internal.status_code == 200, internal.content
        assert pro.status_code == 200, pro.content
        assert internal.json() == pro.json()

        names = [t["name"] for t in internal.json()["data"]["templates"]]
        # Положительная стража: оба шаблона, черновой (PROVISIONAL) тоже — §130.
        assert names == ["1798 Аппаратный маникюр", "1798 Японский маникюр"]

    def test_category_validation_is_inherited(self, templates):
        assert _api().get(TEMPLATES_URL).status_code == 400
        assert _api().get(f"{TEMPLATES_URL}?category_id=not-a-uuid").status_code == 400
        assert (
            _api().get(f"{TEMPLATES_URL}?category_id=00000000-0000-0000-0000-000000000000").status_code
            == 404
        )

    def test_the_pro_route_still_refuses_the_bot_bearer(self, templates):
        """Граница не размыта в другую сторону: Bearer бота на Pro-маршруте — не пропуск."""
        assert _api().get(f"{PRO_TEMPLATES_URL}?category_id={templates.id}").status_code in (401, 403)
