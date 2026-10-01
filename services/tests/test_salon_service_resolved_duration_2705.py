"""Дверь salon-services отдаёт РАЗРЕШЁННУЮ длительность — DRF-2705.

Контракт (`docs/CATALOG_INTERNAL_API_CONTRACT.md`, §1) говорил о
``duration_minutes``: «null ⇒ resolves from template». Дверь при этом отдавала
только сырое значение и UUID шаблона. Потребителю без таблицы шаблонов — а это
зеркало бота — разрешать было нечем: он получал ``null``, записывал «длительности
нет» и отказывал в слотах по услуге, которую каталог продаёт.

Что держат эти узлы:

* ``resolved_duration`` на двери — каскад салон → шаблон, по всем веткам;
* ``duration_minutes`` остаётся СЫРЫМ: null салона не подменяется итогом;
* ``null`` в ``resolved_duration`` означает только «разрешать нечем»;
* каскад ребра (мастер → салон → шаблон) не изменился от того, что его
  салонная половина переехала в ``SalonService.resolved_duration``;
* новое поле не добавляет запросов на строку.

Числа в узлах — литералы, а не значения, взятые из того же кода, что проверяется.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from services.models import (
    SalonService,
    ServiceCategory,
    ServiceTemplate,
    SpecialistService,
)
from tenants.models import Tenant
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-2705"
SALON_URL = "/api/v1/internal/catalog/salon-services/"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def tenant():
    return Tenant.objects.create(slug="d2705-t", name="D2705 Tenant")


@pytest.fixture
def category():
    return ServiceCategory.objects.create(name="D2705 Cat", slug="d2705-cat")


def _template(category, *, duration_default, name="D2705 Tpl"):
    return ServiceTemplate.objects.create(
        category=category, name=name, name_short=name[:10],
        duration_default=duration_default,
    )


def _salon_service(tenant, category, *, template, duration_minutes, name="D2705 Service"):
    return SalonService.objects.create(
        tenant=tenant, template=template, category=category,
        name=name, duration_minutes=duration_minutes,
    )


def _api() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    return c


def _detail(body):
    return body.get("data", body) if isinstance(body, dict) else body


def _rows(body):
    payload = _detail(body)
    if isinstance(payload, dict) and "results" in payload:
        return payload["results"]
    return payload


def _get(service: SalonService) -> dict:
    r = _api().get(f"{SALON_URL}{service.id}/")
    assert r.status_code == 200, r.content
    return _detail(r.json())


# --------------------------------------------------------------------------- #
# Каскад салон → шаблон на двери
# --------------------------------------------------------------------------- #
class TestResolvedDurationOnTheDoor:
    def test_null_salon_duration_resolves_from_the_template(self, tenant, category):
        """Случай, ради которого лист: у салона null, у шаблона 60."""
        service = _salon_service(
            tenant, category, template=_template(category, duration_default=60),
            duration_minutes=None,
        )
        body = _get(service)
        assert body["resolved_duration"] == 60
        # Сырое поле не подменено итогом: это слово салона, а салон промолчал.
        assert body["duration_minutes"] is None

    def test_salon_duration_wins_over_the_template(self, tenant, category):
        service = _salon_service(
            tenant, category, template=_template(category, duration_default=60),
            duration_minutes=30,
        )
        body = _get(service)
        assert body["resolved_duration"] == 30
        assert body["duration_minutes"] == 30

    def test_salon_duration_without_a_template(self, tenant, category):
        """Услуга вне таксономии: шаблона нет, своя длительность есть."""
        service = _salon_service(tenant, category, template=None, duration_minutes=45)
        body = _get(service)
        assert body["resolved_duration"] == 45
        assert body["template"] is None

    def test_nothing_resolves_without_a_template(self, tenant, category):
        service = _salon_service(tenant, category, template=None, duration_minutes=None)
        body = _get(service)
        assert body["resolved_duration"] is None
        assert body["duration_minutes"] is None

    def test_nothing_resolves_from_a_template_without_timing(self, tenant, category):
        """Шаблон есть, но его длительность ещё не выверена (nullable по модели)."""
        service = _salon_service(
            tenant, category, template=_template(category, duration_default=None),
            duration_minutes=None,
        )
        body = _get(service)
        assert body["resolved_duration"] is None

    def test_the_list_carries_it_too(self, tenant, category):
        """Зеркало читает СПИСОК, а не карточку: поле обязано быть и там."""
        template = _template(category, duration_default=60)
        from_template = _salon_service(
            tenant, category, template=template, duration_minutes=None, name="From template",
        )
        own = _salon_service(
            tenant, category, template=template, duration_minutes=30, name="Own",
        )
        r = _api().get(SALON_URL, {"tenant": str(tenant.id)})
        assert r.status_code == 200, r.content
        by_id = {row["id"]: row for row in _rows(r.json())}
        assert by_id[str(from_template.id)]["resolved_duration"] == 60
        assert by_id[str(from_template.id)]["duration_minutes"] is None
        assert by_id[str(own.id)]["resolved_duration"] == 30


# --------------------------------------------------------------------------- #
# Каскад ребра не изменился
# --------------------------------------------------------------------------- #
@pytest.fixture
def specialist(tenant):
    u = User.objects.create_user(
        username="d2705_spec", password="x", role="specialist",
        phone="+79995602705",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.save()
    return p


class TestEdgeCascadeIsUnchanged:
    """``SpecialistService.resolved_duration`` теперь зовёт салонную половину.

    Порядок «мастер → салон → шаблон» записан в контракте (§2) и на нём стоит
    запись; три литерала ниже — три ступени каскада, по одной на узел.
    """

    def _edge(self, salon_service, specialist, *, duration_minutes, is_active=True):
        return SpecialistService.objects.create(
            salon_service=salon_service, specialist=specialist,
            duration_minutes=duration_minutes, price=Decimal("1500"),
            is_active=is_active,
        )

    def test_specialist_override_wins(self, tenant, category, specialist):
        salon = _salon_service(
            tenant, category, template=_template(category, duration_default=60),
            duration_minutes=30,
        )
        assert self._edge(salon, specialist, duration_minutes=45).resolved_duration() == 45

    def test_falls_to_the_salon(self, tenant, category, specialist):
        salon = _salon_service(
            tenant, category, template=_template(category, duration_default=60),
            duration_minutes=30,
        )
        assert self._edge(salon, specialist, duration_minutes=None).resolved_duration() == 30

    def test_falls_to_the_template(self, tenant, category, specialist):
        salon = _salon_service(
            tenant, category, template=_template(category, duration_default=60),
            duration_minutes=None,
        )
        assert self._edge(salon, specialist, duration_minutes=None).resolved_duration() == 60

    def test_nothing_resolves(self, tenant, category, specialist):
        salon = _salon_service(tenant, category, template=None, duration_minutes=None)
        # Неактивное ребро: активное без разрешимой длительности модель не сохранит.
        edge = self._edge(salon, specialist, duration_minutes=None, is_active=False)
        assert edge.resolved_duration() is None


# --------------------------------------------------------------------------- #
# Поле не стоит запроса на строку
# --------------------------------------------------------------------------- #
def test_resolved_duration_adds_no_query_per_row(tenant, category):
    """Шаблон приезжает через ``select_related`` вьюсета — запросов на строку нет.

    Сравнение, а не абсолютное число: сколько запросов делает дверь на одну
    строку, столько же она обязана делать на четыре.
    """
    template = _template(category, duration_default=60)
    _salon_service(tenant, category, template=template, duration_minutes=None, name="S1")

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as one_row:
        r = _api().get(SALON_URL, {"tenant": str(tenant.id)})
        assert r.status_code == 200 and len(_rows(r.json())) == 1

    for i in range(2, 5):
        _salon_service(
            tenant, category, template=_template(category, duration_default=60, name=f"T{i}"),
            duration_minutes=None, name=f"S{i}",
        )

    with CaptureQueriesContext(connection) as four_rows:
        r = _api().get(SALON_URL, {"tenant": str(tenant.id)})
        assert r.status_code == 200 and len(_rows(r.json())) == 4
        assert all(row["resolved_duration"] == 60 for row in _rows(r.json()))

    assert len(four_rows) == len(one_row), (
        f"1 строка — {len(one_row)} запросов, 4 строки — {len(four_rows)}: "
        "поле тянет шаблон отдельным запросом на строку"
    )
