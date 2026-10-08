"""DRF-2893 — салон у мастера в админке назначают один раз.

Поле «Салон» на форме мастера редактировалось руками и выглядело как
«перевести мастера в другой салон». Перевода в каталоге нет: правка меняла
одну ссылку. Строка отношения (доступ), предложения мастера и место
оставались за прежним салоном, а следующее сохранение старого предложения
падало на проверке «тот же салон».

Узлы держат:

* назначенный салон формой не меняется — ни полем (его в форме нет), ни
  подделанным POST;
* пустой салон назначить можно: так оператор заводит мастера (DRF-1596);
* прочие поля мастера правятся как раньше;
* подсказка раздела говорит, почему поле закрыто.

Пределы: закрыта только форма админки. Правку через ORM узлы не держат.
"""

from __future__ import annotations

import pytest
from django.contrib import admin as django_admin
from django.test import Client, RequestFactory
from django.urls import reverse

from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


@pytest.fixture
def operator(db):
    return User.objects.create_superuser(
        username="operator-2893", password="pw",  # pragma: allowlist secret
        email="o@b.c", role="admin",
    )


@pytest.fixture
def salons(db):
    return (
        Tenant.objects.create(slug="salon-a-2893", name="Салон А"),
        Tenant.objects.create(slug="salon-b-2893", name="Салон Б"),
    )


def _master(name: str, tenant=None) -> SpecialistProfile:
    user = User.objects.create_user(username=f"master-{name}", password="x", role="specialist")
    profile = SpecialistProfile.objects.get(user=user)
    SpecialistProfile.objects.filter(pk=profile.pk).update(display_name=name, tenant=tenant)
    profile.refresh_from_db()
    return profile


def _model_admin():
    return django_admin.site._registry[SpecialistProfile]


def _form_fields(operator, profile) -> set[str]:
    request = RequestFactory().get("/")
    request.user = operator
    return set(_model_admin().get_form(request, profile, change=True).base_fields)


def _post(operator, profile, **over):
    """Настоящий POST формы изменения: поля — из самой формы, как их шлёт браузер."""
    client = Client()
    client.force_login(operator)
    url = reverse("admin:users_specialistprofile_change", args=[profile.pk])
    page = client.get(url)
    assert page.status_code == 200
    data = {}
    for name, field in page.context["adminform"].form.fields.items():
        value = page.context["adminform"].form.initial.get(name, field.initial)
        if not value and value != 0:  # пустое, снятый флажок, файл без файла
            continue
        data[name] = getattr(value, "pk", value)
    for formset in page.context["inline_admin_formsets"]:
        management = formset.formset.management_form
        for name in management.fields:
            data[f"{management.prefix}-{name}"] = management.initial.get(name, 0) or 0
    data.update(over)
    return client.post(url, data)


def test_an_assigned_salon_is_not_a_form_field(operator, salons) -> None:
    salon_a, _ = salons
    master = _master("assigned", salon_a)

    assert "tenant" not in _form_fields(operator, master)
    request = RequestFactory().get("/")
    request.user = operator
    assert "tenant" in _model_admin().get_readonly_fields(request, master)


def test_an_empty_salon_can_still_be_assigned(operator, salons) -> None:
    salon_a, _ = salons
    master = _master("unassigned")
    assert "tenant" in _form_fields(operator, master)

    response = _post(operator, master, tenant=str(salon_a.pk))

    assert response.status_code == 302, response.context and response.context["adminform"].form.errors
    master.refresh_from_db()
    assert master.tenant_id == salon_a.pk


def test_a_forged_post_does_not_move_the_master(operator, salons) -> None:
    salon_a, salon_b = salons
    master = _master("forged", salon_a)

    response = _post(operator, master, tenant=str(salon_b.pk), display_name="Переименован")

    assert response.status_code == 302, response.context and response.context["adminform"].form.errors
    master.refresh_from_db()
    # Положительная пара: форма сохранилась — имя сменилось, салон остался.
    assert master.display_name == "Переименован"
    assert master.tenant_id == salon_a.pk


def test_a_new_master_form_offers_the_salon(operator) -> None:
    request = RequestFactory().get("/")
    request.user = operator

    assert "tenant" in set(_model_admin().get_form(request).base_fields)
    assert "tenant" not in _model_admin().get_readonly_fields(request)


def test_the_section_says_why_the_salon_is_closed() -> None:
    description = dict(_model_admin().fieldsets)["Салон"]["description"]

    assert "не меняется" in description
    assert "перевода мастера между салонами в каталоге нет" in description
