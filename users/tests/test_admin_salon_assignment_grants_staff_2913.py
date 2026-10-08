"""DRF-2913 — оператор назначил мастеру салон: у мастера есть доступ сотрудника.

Связь «мастер ↔ салон» хранится в двух местах: салон у профиля (каталог и
запись) и активная строка отношения с ролью (доступ к записям и расписанию
салона). Админка писала только первую. Замер пилота 08.10: четыре
продаваемых мастера одного салона без единой строки отношения — клиенту
они показывались, а расписание салона им было закрыто.

Узлы держат на ОБОИХ входах админки — форме мастера и блоке мастеров на
форме салона:

* назначение салона заводит активную строку staff с пометкой «выдано
  админом»;
* отозванного форма не возвращает: отказ по полю, салон не назначается;
* клиент салона, ставший мастером, меняет роль — строка активная одна;
* уже работающему в салоне вторая строка не заводится;
* правка мастера БЕЗ назначения салона доступа не раздаёт — уже
  заведённых мастеров без связи форма не чинит побочным эффектом.

Пределы: запись мимо админки (ORM, ``update()``) связь не заводит.
"""

from __future__ import annotations

import pytest
from django.test import Client
from django.urls import reverse

from tenants.models import Tenant
from users.master_salon_link import (
    ROLE_CHANGE_REASON, MasterWasRevokedError, grant_staff_on_admin_assignment, was_revoked_from,
)
from users.models import SpecialistProfile, TenantUserRelationship, User

pytestmark = pytest.mark.django_db

Role = TenantUserRelationship.Role


@pytest.fixture
def operator(db):
    return User.objects.create_superuser(
        username="operator-2913", password="pw",  # pragma: allowlist secret
        email="o13@b.c", role="admin",
    )


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="salon-2913", name="Салон 2913", city="Пенза", address="Пенза, Московская, 2")


def _master(name: str, tenant=None) -> SpecialistProfile:
    """Мастер так, как он выглядит после формы пользователя: профиль есть, салона и связей нет."""
    user = User.objects.create_user(username=f"m13-{name}", password="x", role="specialist")
    profile = SpecialistProfile.objects.get(user=user)
    SpecialistProfile.objects.filter(pk=profile.pk).update(display_name=name, tenant=tenant)
    TenantUserRelationship.objects.filter(user=user).delete()
    profile.refresh_from_db()
    return profile


def _rows(profile, tenant) -> list[tuple]:
    return sorted(
        TenantUserRelationship.objects.filter(user=profile.user, tenant=tenant)
        .values_list("role", "is_active", "granted_by", "revoke_reason")
    )


def _revoke(profile, tenant, role=Role.STAFF) -> None:
    TenantUserRelationship.objects.create(
        user=profile.user, tenant=tenant, role=role, is_active=False, revoke_reason="master_left",
    )


def _page_data(page) -> dict:
    """Поля формы изменения так, как их шлёт браузер, вместе с вложенными блоками."""
    data = {}
    form = page.context["adminform"].form
    for name, field in form.fields.items():
        value = form.initial.get(name, field.initial)
        if not value and value != 0:
            continue
        data[name] = getattr(value, "pk", value)
    for inline in page.context["inline_admin_formsets"]:
        formset = inline.formset
        for name in formset.management_form.fields:
            data[f"{formset.prefix}-{name}"] = formset.management_form.initial.get(name, 0) or 0
        # Пустую «лишнюю» строку блока браузер шлёт с умолчаниями полей;
        # здесь её просто нет — в счёт идут только существующие строки.
        data[f"{formset.prefix}-TOTAL_FORMS"] = len(formset.initial_forms)
        for bound in formset.initial_forms:
            for name, field in bound.fields.items():
                value = bound.initial.get(name, field.initial)
                if not value and value != 0:
                    continue
                data[f"{bound.prefix}-{name}"] = getattr(value, "pk", value)
            data[f"{bound.prefix}-{formset.model._meta.pk.name}"] = bound.instance.pk
    return data


def _post_master_form(operator, profile, **over):
    client = Client()
    client.force_login(operator)
    url = reverse("admin:users_specialistprofile_change", args=[profile.pk])
    page = client.get(url)
    assert page.status_code == 200
    return client.post(url, {**_page_data(page), **over})


def _post_salon_row(operator, salon, user, **over):
    """Форма салона с одной новой строкой в блоке мастеров."""
    client = Client()
    client.force_login(operator)
    url = reverse("admin:tenants_tenant_change", args=[salon.pk])
    page = client.get(url)
    assert page.status_code == 200
    data = _page_data(page)
    index = int(data["specialist_profiles-TOTAL_FORMS"])
    data["specialist_profiles-TOTAL_FORMS"] = index + 1
    row = f"specialist_profiles-{index}"
    data.update({
        f"{row}-id": "", f"{row}-tenant": str(salon.pk), f"{row}-user": str(user.pk),
        f"{row}-display_name": "Мастер из блока", f"{row}-experience_years": "3",
        f"{row}-status": SpecialistProfile.ProfileStatus.ACTIVE,
        f"{row}-is_available": "on", f"{row}-is_booking_enabled": "on",
        f"{row}-timezone": "Europe/Moscow",
        f"{row}-booking_source": SpecialistProfile.BookingSource.AYLA_LOCAL,
    })
    data.update(over)
    return client.post(url, data)


def _errors(response) -> str:
    if response.status_code == 302:
        return ""
    found = [str(response.context["adminform"].form.errors)]
    found += [str(inline.formset.errors) for inline in response.context["inline_admin_formsets"]]
    return " ".join(found)


# ─── форма мастера ───────────────────────────────────────────────────────────


def test_assigning_a_salon_on_the_master_form_grants_staff(operator, salon) -> None:
    master = _master("form")
    assert _rows(master, salon) == []

    response = _post_master_form(operator, master, tenant=str(salon.pk))

    assert response.status_code == 302, _errors(response)
    master.refresh_from_db()
    assert master.tenant_id == salon.pk
    assert _rows(master, salon) == [(Role.STAFF, True, TenantUserRelationship.GrantedBy.ADMIN, "")]


def test_the_master_form_does_not_bring_back_a_revoked_master(operator, salon) -> None:
    master = _master("revoked")
    _revoke(master, salon)

    response = _post_master_form(operator, master, tenant=str(salon.pk))

    assert response.status_code == 200
    codes = [e.code for e in response.context["adminform"].form.errors.as_data().get("tenant", [])]
    assert codes == ["master_was_revoked_from_this_tenant"]
    master.refresh_from_db()
    assert master.tenant_id is None
    assert not TenantUserRelationship.objects.filter(user=master.user, tenant=salon, is_active=True).exists()


def test_editing_a_master_without_assigning_a_salon_grants_nothing(operator, salon) -> None:
    """Уже заведённого мастера без связи форма не чинит побочным эффектом."""
    master = _master("legacy", tenant=salon)

    response = _post_master_form(operator, master, display_name="Переименован")

    assert response.status_code == 302, _errors(response)
    master.refresh_from_db()
    assert master.display_name == "Переименован"  # положительная пара: форма сохранилась
    assert _rows(master, salon) == []


def test_a_customer_of_the_salon_becomes_its_master_by_a_role_change(operator, salon) -> None:
    master = _master("customer")
    TenantUserRelationship.objects.create(user=master.user, tenant=salon, role=Role.CUSTOMER)

    response = _post_master_form(operator, master, tenant=str(salon.pk))

    assert response.status_code == 302, _errors(response)
    assert _rows(master, salon) == [
        (Role.CUSTOMER, False, TenantUserRelationship.GrantedBy.SELF, ROLE_CHANGE_REASON),
        (Role.STAFF, True, TenantUserRelationship.GrantedBy.ADMIN, ""),
    ]


def test_someone_already_working_in_the_salon_gets_no_second_row(operator, salon) -> None:
    master = _master("admin")
    TenantUserRelationship.objects.create(user=master.user, tenant=salon, role=Role.ADMIN)

    response = _post_master_form(operator, master, tenant=str(salon.pk))

    assert response.status_code == 302, _errors(response)
    assert [row[:2] for row in _rows(master, salon)] == [(Role.ADMIN, True)]


# ─── блок мастеров на форме салона ───────────────────────────────────────────


def test_a_row_in_the_salon_masters_block_grants_staff(operator, salon) -> None:
    master = _master("inline")

    response = _post_salon_row(operator, salon, master.user)

    assert response.status_code == 302, _errors(response)
    master.refresh_from_db()
    assert master.tenant_id == salon.pk
    assert _rows(master, salon) == [(Role.STAFF, True, TenantUserRelationship.GrantedBy.ADMIN, "")]


def test_the_salon_masters_block_does_not_bring_back_a_revoked_master(operator, salon) -> None:
    master = _master("inline-revoked")
    _revoke(master, salon)

    response = _post_salon_row(operator, salon, master.user)

    assert response.status_code == 200
    assert "была отозвана" in _errors(response)
    master.refresh_from_db()
    assert master.tenant_id is None
    assert not TenantUserRelationship.objects.filter(user=master.user, tenant=salon, is_active=True).exists()


def test_saving_the_salon_with_its_existing_masters_grants_nothing(operator, salon) -> None:
    """Строка мастера, уже числящегося в салоне, — не назначение."""
    legacy = _master("inline-legacy", tenant=salon)
    client = Client()
    client.force_login(operator)
    url = reverse("admin:tenants_tenant_change", args=[salon.pk])
    page = client.get(url)

    response = client.post(url, {**_page_data(page), "name": "Салон 2913, переименован"})

    assert response.status_code == 302, _errors(response)
    salon.refresh_from_db()
    assert salon.name == "Салон 2913, переименован"  # положительная пара
    assert _rows(legacy, salon) == []


# ─── сама операция ───────────────────────────────────────────────────────────


def test_revoked_means_a_retired_row_and_no_active_one(salon) -> None:
    master = _master("unit")
    assert was_revoked_from(master.user, salon.pk) is False

    _revoke(master, salon)
    assert was_revoked_from(master.user, salon.pk) is True

    TenantUserRelationship.objects.create(user=master.user, tenant=salon, role=Role.STAFF)
    assert was_revoked_from(master.user, salon.pk) is False


def test_the_operation_refuses_a_revoked_master_and_writes_nothing(salon) -> None:
    master = _master("unit-revoked")
    _revoke(master, salon)

    with pytest.raises(MasterWasRevokedError):
        grant_staff_on_admin_assignment(master.user, salon.pk)

    assert [row[:2] for row in _rows(master, salon)] == [(Role.STAFF, False)]


def test_the_operation_is_idempotent(salon) -> None:
    master = _master("unit-twice")

    assert grant_staff_on_admin_assignment(master.user, salon.pk) is True
    assert grant_staff_on_admin_assignment(master.user, salon.pk) is False

    assert len(_rows(master, salon)) == 1


def test_a_revoke_in_another_salon_does_not_block_this_one(salon) -> None:
    other = Tenant.objects.create(slug="other-2913", name="Другой")
    master = _master("unit-other")
    _revoke(master, other)

    assert grant_staff_on_admin_assignment(master.user, salon.pk) is True
