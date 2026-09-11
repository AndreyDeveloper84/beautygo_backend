"""`ServiceLocation` — место оказания услуги (§9, DRF-1687, срез L1).

Что закрепляется:

* восемь полей происхождения координат — те же имена, что у ``Tenant`` (#333):
  перенос, а не вторая редакция;
* три статуса места (§9 дословно), умолчание ``review_required``;
* подтверждение без автора/времени/основания невозможно **схемой**, не
  только формой — проверяется обходом ``clean()`` через ``objects.create``;
* половина пары и (0, 0) — тоже схемой;
* в расстоянии участвует только ``confirmed ∧ is_geocoded`` — с положительной
  стражей и обеими отрицательными;
* «совпавшие точки без дублей»: уникальность по (салон, нормализованный адрес),
  и для соло (``tenant IS NULL``) тоже — ``nulls_distinct=False``;
* миграция только создаёт таблицу — ни одной операции с данными (§9:
  «массовый перенос запрещён»).
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone as dt_tz
from decimal import Decimal as D
from pathlib import Path

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from tenants.models import GeocodeStatus, LocationStatus, ServiceLocation, Tenant
from users.models import User

PENZA = (D("53.195878"), D("45.018316"))
PROVENANCE_FIELDS = (
    "geocode_source_address", "geocode_normalized_address", "latitude", "longitude",
    "geocode_provider", "geocode_precision", "geocode_status", "geocoded_at",
)


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="loc-t", name="Loc")


@pytest.fixture
def operator(db):
    return User.objects.create_user(username="loc-op", password="x", phone="+79990001687")


def _confirmed(**kw):
    base = dict(status=LocationStatus.CONFIRMED, confirmed_at=datetime(2026, 9, 11, tzinfo=dt_tz.utc),
                confirmed_source_ref="decisions-2026-09-11 §10")
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Форма: перенос, не редакция
# ---------------------------------------------------------------------------


def test_the_eight_provenance_fields_are_the_same_as_on_tenant():
    """Имена совпадают с Tenant буквально: адаптер меняет цель записи, а не поля."""
    tenant_fields = {f.name for f in Tenant._meta.get_fields()}
    loc_fields = {f.name for f in ServiceLocation._meta.get_fields()}
    assert set(PROVENANCE_FIELDS) <= tenant_fields
    assert set(PROVENANCE_FIELDS) <= loc_fields


def test_three_location_statuses_and_the_default_is_review_required():
    assert {s.value for s in LocationStatus} == {"confirmed", "review_required", "inactive"}
    assert ServiceLocation._meta.get_field("status").default == LocationStatus.REVIEW_REQUIRED


def test_migration_only_creates_the_table_and_moves_no_data():
    """§9: «автоматический массовый перенос запрещён» — и в миграции его нет."""
    src = Path(__file__).resolve().parents[1] / "migrations" / "0006_service_location.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    ops = [
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "migrations"
        and node.func.attr != "swappable_dependency"  # это зависимость, не операция
    ]
    assert ops == ["CreateModel"], ops


# ---------------------------------------------------------------------------
# Подтверждение — с провенансом, схемой
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_confirmed_without_provenance_is_refused_by_clean(tenant):
    loc = ServiceLocation(tenant=tenant, address="ул Пушкина, д 45", status=LocationStatus.CONFIRMED)
    with pytest.raises(ValidationError) as exc:
        loc.full_clean()
    assert {"confirmed_by", "confirmed_at", "confirmed_source_ref"} <= set(exc.value.message_dict)


@pytest.mark.django_db
def test_confirmed_without_provenance_is_refused_by_the_schema_too(tenant):
    """Мимо формы — objects.create без clean() — отказывает база."""
    with pytest.raises(IntegrityError, match="servicelocation_confirmed_requires_provenance"):
        with transaction.atomic():
            ServiceLocation.objects.create(tenant=tenant, address="ул Пушкина, д 45",
                                           status=LocationStatus.CONFIRMED)


@pytest.mark.django_db
def test_confirmed_with_provenance_round_trips(tenant, operator):
    loc = ServiceLocation(tenant=tenant, address="ул Пушкина, д 45", city="Пенза",
                          **_confirmed(confirmed_by=operator))
    loc.full_clean()
    loc.save()
    fresh = ServiceLocation.objects.get(pk=loc.pk)
    assert fresh.status == LocationStatus.CONFIRMED and fresh.confirmed_by == operator
    assert str(fresh) == "ул Пушкина, д 45 (loc-t)"


@pytest.mark.django_db
def test_a_location_without_an_address_is_not_a_location(tenant):
    with pytest.raises(ValidationError, match="Место без адреса"):
        ServiceLocation(tenant=tenant, address="   ").full_clean()


# ---------------------------------------------------------------------------
# Координаты — те же отказы, что у Tenant, и схемой
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_half_a_pair_is_refused_by_the_schema(tenant):
    with pytest.raises(IntegrityError, match="servicelocation_coordinates_are_a_pair"):
        with transaction.atomic():
            ServiceLocation.objects.create(tenant=tenant, address="x", latitude=PENZA[0])


@pytest.mark.django_db
def test_zero_zero_is_refused_by_the_schema(tenant):
    with pytest.raises(IntegrityError, match="servicelocation_no_zero_zero"):
        with transaction.atomic():
            ServiceLocation.objects.create(tenant=tenant, address="x", latitude=D("0"), longitude=D("0"))


@pytest.mark.parametrize("status, lat, lng, expected", [
    (GeocodeStatus.OK, *PENZA, True),
    (GeocodeStatus.CONFIRMED, *PENZA, True),
    (GeocodeStatus.PENDING, *PENZA, False),      # pending с числами — не геокодировано (§139)
    (GeocodeStatus.AMBIGUOUS, *PENZA, False),
    (GeocodeStatus.OK, None, None, False),
])
def test_is_geocoded_is_the_same_rule_as_on_tenant(status, lat, lng, expected):
    loc = ServiceLocation(address="x", geocode_status=status, latitude=lat, longitude=lng)
    assert loc.is_geocoded is expected


def test_only_a_confirmed_and_geocoded_place_participates_in_distance(operator):
    """Две оси сходятся в одном свойстве; обе отрицательные стороны и положительная."""
    live = ServiceLocation(address="x", geocode_status=GeocodeStatus.OK, latitude=PENZA[0], longitude=PENZA[1],
                           **_confirmed(confirmed_by=operator))
    assert live.participates_in_distance
    unconfirmed = ServiceLocation(address="x", geocode_status=GeocodeStatus.OK,
                                  latitude=PENZA[0], longitude=PENZA[1])
    assert not unconfirmed.participates_in_distance
    ungeocoded = ServiceLocation(address="x", geocode_status=GeocodeStatus.PENDING,
                                 latitude=PENZA[0], longitude=PENZA[1], **_confirmed(confirmed_by=operator))
    assert not ungeocoded.participates_in_distance
    inactive = ServiceLocation(address="x", geocode_status=GeocodeStatus.OK, latitude=PENZA[0],
                               longitude=PENZA[1], status=LocationStatus.INACTIVE)
    assert not inactive.participates_in_distance


# ---------------------------------------------------------------------------
# Совпавшие точки без дублей
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_same_point_for_one_tenant_is_refused(tenant):
    ServiceLocation.objects.create(tenant=tenant, address="a", geocode_normalized_address="г пенза, ул пушкина, д 45")
    with pytest.raises(IntegrityError, match="servicelocation_point_is_unique"):
        with transaction.atomic():
            ServiceLocation.objects.create(tenant=tenant, address="b",
                                           geocode_normalized_address="г пенза, ул пушкина, д 45")


@pytest.mark.django_db
def test_the_same_point_for_two_solo_masters_is_refused_too(db):
    """tenant IS NULL участвует в уникальности как значение: иначе у соло дубли не ловились бы."""
    ServiceLocation.objects.create(address="a", geocode_normalized_address="г пенза, ул кирова, д 20")
    with pytest.raises(IntegrityError, match="servicelocation_point_is_unique"):
        with transaction.atomic():
            ServiceLocation.objects.create(address="b", geocode_normalized_address="г пенза, ул кирова, д 20")


@pytest.mark.django_db
def test_ungeocoded_places_do_not_collide(tenant):
    """Пока адрес не нормализован, точки нет — и сравнивать нечего."""
    ServiceLocation.objects.create(tenant=tenant, address="ул Пушкина 45")
    ServiceLocation.objects.create(tenant=tenant, address="улица Пушкина, д. 45")
    assert ServiceLocation.objects.filter(tenant=tenant).count() == 2


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------


def test_admin_is_registered_and_geocoder_fields_are_read_only():
    from tenants.admin import ServiceLocationAdmin, ServiceLocationInline, TenantAdmin
    assert ServiceLocation in admin.site._registry
    assert set(PROVENANCE_FIELDS) <= set(ServiceLocationAdmin.readonly_fields)
    assert ServiceLocationInline in TenantAdmin.inlines
    assert "status" in ServiceLocationAdmin.list_filter
