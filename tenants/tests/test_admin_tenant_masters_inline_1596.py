"""Мастера заводятся на форме салона одним заходом (DRF-1596).

Замер 08.09.2026: ``TenantAdmin.inlines`` — пустой кортеж. Владелец открыл
``/admin/tenants/tenant/add/``, чтобы завести настоящий салон с мастерами,
и добавить мастера ему было негде: форма салона про мастеров не знала, а
форма мастера не знала про салон (вторая половина — в
``users/tests/test_admin_specialist_form_1596.py``).

Отдельно закрепляется ловушка, из-за которой наивный вложенный блок
падал бы на первом же сохранении. ``users.signals.create_user_profile``
на создание ``User(role='specialist')`` заводит ``SpecialistProfile``
сам. Значит к моменту, когда оператор возвращается на форму салона и
выбирает этого пользователя в строке блока, профиль на него уже есть, а
связь ``user`` — ``OneToOneField``. Обычный inline попытался бы вставить
вторую строку и получил бы нарушение уникальности вместо салона с
мастерами. Блок обязан подхватывать существующий профиль, а не плодить
второй, — и обязан отказываться подхватывать чужого мастера.
"""
from __future__ import annotations

import pytest
from django.contrib import admin as django_admin
from django.urls import reverse

from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


def _tenant_admin():
    return django_admin.site._registry[Tenant]


def _master_inline():
    for inline in _tenant_admin().inlines:
        if inline.model is SpecialistProfile:
            return inline
    return None


def test_tenant_form_has_a_masters_inline():
    """На форме салона есть вложенный блок мастеров."""
    assert _master_inline() is not None, (
        "TenantAdmin.inlines не содержит блока по SpecialistProfile: "
        f"{_tenant_admin().inlines}"
    )


def test_masters_inline_exposes_the_fields_that_decide_visibility():
    """Блок не заводит невидимого мастера втихую.

    ``status`` по умолчанию ``draft``, а каталог отдаёт только
    ``status=active`` И ``is_available=True``. Если блок этих полей не
    показывает, мастер, заведённый на форме салона, не доедет до
    клиента — ровно тот молчаливый отказ, против которого заведён тикет.

    Подсказка требуется здесь не меньше, чем на отдельной форме мастера:
    салон заводят именно на этом экране.
    """
    inline = _master_inline()
    assert inline is not None
    fields: set[str] = set()
    explained: set[str] = set()
    for _title, opts in inline.fieldsets:
        fields.update(opts["fields"])
        if (opts.get("description") or "").strip():
            explained.update(opts["fields"])
    for name in ("user", "display_name", "status", "is_available",
                 "is_booking_enabled", "timezone", "booking_source"):
        assert name in fields, f"{name} нет в блоке мастеров: {sorted(fields)}"
    for name in ("status", "is_available", "is_booking_enabled",
                 "timezone", "booking_source"):
        assert name in explained, f"{name} в блоке без подсказки"


@pytest.mark.no_auto_tenant
def test_salon_and_master_are_created_in_one_pass(client):
    """Салон и мастер заводятся одним сохранением формы салона."""
    staff = User.objects.create_superuser(
        username="drf1596-inline-admin", password="pw",  # pragma: allowlist secret
        email="i@b.c",
        role="admin",
    )
    client.force_login(staff)

    # Страница обязана открываться: блок тянет автодополнение по
    # пользователям, и опечатка в его настройке ломает форму целиком, а
    # не одно поле.
    assert client.get(reverse("admin:tenants_tenant_add")).status_code == 200

    # Пользователь-мастер уже заведён формой пользователя — и сигнал
    # заодно завёл ему профиль без салона.
    master_user = User.objects.create_user(
        username="drf1596-inline-master", password="x",  # pragma: allowlist secret
        role="specialist", phone="+79001596002",
    )
    existing = SpecialistProfile.objects.get(user=master_user)

    resp = client.post(
        reverse("admin:tenants_tenant_add"),
        {
            "slug": "drf1596-inline-salon",
            "name": "Салон с мастерами",
            "city": "Пенза",
            "address": "Пенза, ул. Московская, 2",
            "is_active": "on",
            "specialist_profiles-TOTAL_FORMS": "1",
            "specialist_profiles-INITIAL_FORMS": "0",
            "specialist_profiles-MIN_NUM_FORMS": "0",
            "specialist_profiles-MAX_NUM_FORMS": "1000",
            # L1 DRF-1687: у салона появился второй вложенный блок — места
            # оказания услуг; браузер шлёт его management-форму всегда.
            "locations-TOTAL_FORMS": "0",
            "locations-INITIAL_FORMS": "0",
            "locations-MIN_NUM_FORMS": "0",
            "locations-MAX_NUM_FORMS": "1000",
            "specialist_profiles-0-id": "",
            "specialist_profiles-0-tenant": "",
            "specialist_profiles-0-user": str(master_user.id),
            "specialist_profiles-0-display_name": "Ольга",
            "specialist_profiles-0-experience_years": "5",
            "specialist_profiles-0-status": SpecialistProfile.ProfileStatus.ACTIVE,
            "specialist_profiles-0-is_available": "on",
            "specialist_profiles-0-is_booking_enabled": "on",
            "specialist_profiles-0-timezone": "Europe/Moscow",
            "specialist_profiles-0-booking_source":
                SpecialistProfile.BookingSource.AYLA_LOCAL,
        },
    )
    assert resp.status_code == 302, resp.content[:4000]

    tenant = Tenant.all_objects.get(slug="drf1596-inline-salon")
    # Вторая строка на того же человека не заведена — подхвачена первая.
    profiles = list(SpecialistProfile.objects.filter(user=master_user))
    assert len(profiles) == 1
    assert profiles[0].pk == existing.pk
    assert profiles[0].tenant_id == tenant.id
    assert profiles[0].display_name == "Ольга"
    assert profiles[0].status == SpecialistProfile.ProfileStatus.ACTIVE


@pytest.mark.no_auto_tenant
def test_inline_refuses_to_steal_a_master_from_another_salon(client):
    """Подхват — не кража.

    Подхватывать разрешено только профиль без салона или профиль этого
    же салона. Иначе форма чужого салона молча перевела бы мастера к
    себе, и первый салон потерял бы её без единого следа.
    """
    staff = User.objects.create_superuser(
        username="drf1596-steal-admin", password="pw",  # pragma: allowlist secret
        email="s@b.c",
        role="admin",
    )
    client.force_login(staff)

    other = Tenant.objects.create(slug="drf1596-other", name="Чужой салон")
    master_user = User.objects.create_user(
        username="drf1596-owned-master", password="x",  # pragma: allowlist secret
        role="specialist", phone="+79001596003",
    )
    owned = SpecialistProfile.objects.get(user=master_user)
    owned.tenant = other
    owned.save(update_fields=["tenant"])

    resp = client.post(
        reverse("admin:tenants_tenant_add"),
        {
            "slug": "drf1596-thief",
            "name": "Салон-похититель",
            "is_active": "on",
            "specialist_profiles-TOTAL_FORMS": "1",
            "specialist_profiles-INITIAL_FORMS": "0",
            "specialist_profiles-MIN_NUM_FORMS": "0",
            "specialist_profiles-MAX_NUM_FORMS": "1000",
            # L1 DRF-1687: у салона появился второй вложенный блок — места
            # оказания услуг; браузер шлёт его management-форму всегда.
            "locations-TOTAL_FORMS": "0",
            "locations-INITIAL_FORMS": "0",
            "locations-MIN_NUM_FORMS": "0",
            "locations-MAX_NUM_FORMS": "1000",
            "specialist_profiles-0-id": "",
            "specialist_profiles-0-tenant": "",
            "specialist_profiles-0-user": str(master_user.id),
            "specialist_profiles-0-display_name": "Не отдам",
            "specialist_profiles-0-experience_years": "1",
            "specialist_profiles-0-status": SpecialistProfile.ProfileStatus.ACTIVE,
            "specialist_profiles-0-is_available": "on",
            "specialist_profiles-0-is_booking_enabled": "on",
            "specialist_profiles-0-timezone": "Europe/Moscow",
            "specialist_profiles-0-booking_source":
                SpecialistProfile.BookingSource.AYLA_LOCAL,
        },
    )
    # Форма возвращается с ошибкой, а не 302 на список.
    assert resp.status_code == 200
    assert not Tenant.all_objects.filter(slug="drf1596-thief").exists()
    owned.refresh_from_db()
    assert owned.tenant_id == other.id
