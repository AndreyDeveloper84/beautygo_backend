"""`SpecialistProfile.works_at` — «Master → works_at → ServiceLocation» (§9, DRF-1687, L2).

Что закрепляется:

* поле есть, необязательное, `PROTECT`: место с мастерами не удаляется —
  его переводят в `inactive`, иначе удаление молча снимало бы мастеров с карты;
* место салона — только у мастера этого салона (`clean()`); самостоятельный
  мастер — в месте без салона. Предел назван: `update()` мимо `clean()` это
  обойдёт, CheckConstraint в соседнюю таблицу не смотрит;
* миграция — один `AddField`, без данных: 27 старых адресов НЕ переносятся;
* обе админ-формы мастера (своя и инлайн в салоне) показывают поле.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.db.models import PROTECT, ProtectedError

from tenants.models import LocationStatus, ServiceLocation, Tenant
from users.models import SpecialistProfile, User


@pytest.fixture
def salon_a(db):
    return Tenant.objects.create(slug="wa-a", name="A")


@pytest.fixture
def salon_b(db):
    return Tenant.objects.create(slug="wa-b", name="B")


def _master(tenant, phone):
    u = User.objects.create_user(username=f"m{phone[-4:]}", password="x", role="specialist", phone=phone)
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = f"Мастер {phone[-4:]}"
    p.save()
    return p


def test_field_shape_is_optional_fk_with_protect():
    f = SpecialistProfile._meta.get_field("works_at")
    assert f.null and f.blank
    assert f.remote_field.model is ServiceLocation
    assert f.remote_field.on_delete is PROTECT
    assert f.remote_field.related_name == "masters"


def test_migration_only_adds_the_field_and_moves_no_data():
    """§9: 27 старых адресов мастеров НЕ переносятся — и в миграции нечем."""
    src = Path(__file__).resolve().parents[1] / "migrations" / "0018_specialistprofile_works_at.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    ops = [
        n.func.attr for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "migrations"
        and n.func.attr != "swappable_dependency"
    ]
    assert ops == ["AddField"], ops


@pytest.mark.django_db
def test_salon_master_may_work_at_the_salons_place(salon_a):
    place = ServiceLocation.objects.create(tenant=salon_a, address="ул Пушкина, д 45")
    m = _master(salon_a, "+79990000001")
    m.works_at = place
    m.full_clean(exclude=["user"])
    m.save()
    assert list(place.masters.all()) == [m]


@pytest.mark.django_db
def test_salon_master_may_not_work_at_another_salons_place(salon_a, salon_b):
    """Расстояние до чужой двери — ровно то, что §9 запрещает."""
    place_b = ServiceLocation.objects.create(tenant=salon_b, address="ул Кирова, д 20")
    m = _master(salon_a, "+79990000002")
    m.works_at = place_b
    with pytest.raises(ValidationError) as exc:
        m.full_clean(exclude=["user"])
    assert "works_at" in exc.value.message_dict


@pytest.mark.django_db
def test_solo_master_works_at_a_place_without_a_salon(salon_a):
    """Точка без салона годится любому: и соло без тенанта, и мастеру салона."""
    solo_place = ServiceLocation.objects.create(address="ул Ладожская, д 130")
    solo = _master(None, "+79990000003")
    solo.works_at = solo_place
    solo.full_clean(exclude=["user"])
    solo.save()
    assert solo.works_at == solo_place


@pytest.mark.django_db
def test_a_place_with_masters_cannot_be_deleted_only_deactivated(salon_a):
    place = ServiceLocation.objects.create(tenant=salon_a, address="ул Пушкина, д 45")
    m = _master(salon_a, "+79990000004")
    m.works_at = place
    m.save()
    with pytest.raises(ProtectedError):
        place.delete()
    place.status = LocationStatus.INACTIVE
    place.save(update_fields=["status"])
    m.refresh_from_db()
    assert m.works_at == place and not place.participates_in_distance


def test_both_admin_forms_of_a_master_show_the_field():
    from users.admin import SpecialistProfileAdmin, TenantMastersInline

    def fields_of(fieldsets):
        return {f for _, opts in fieldsets for f in opts["fields"]}

    assert "works_at" in fields_of(SpecialistProfileAdmin.fieldsets)
    assert "works_at" in fields_of(TenantMastersInline.fieldsets)
    assert "works_at" in SpecialistProfileAdmin.raw_id_fields
