"""Slug салона не меняется после создания (решение владельца, раздел Q; аудит 15.09 §3 п.10).

Подсказка поля ``Tenant.slug`` обещает «Cannot be changed after creation», а
админка давала править slug на форме салона (``readonly_fields`` — только id и
даты). Slug — проводной идентификатор: по нему бот привязывает салон
(«Подключить салон», ``tenants/provisioning.py``), по нему ходит заголовок
X-Tenant, к одному slug привязан салонный бот MAX. Переименование молча рвёт
эти привязки.

* админка: на форме существующего салона slug только для чтения, на форме
  добавления — редактируется;
* модель: ``clean()`` отказывает, если slug существующей строки отличается от
  записанного в базе. Ключ ``slug`` безопасен: на форме изменения поля нет, и
  slug там измениться не может, а там, где поле есть, ключ существует — 500 нет.

Названный предел: ``save()`` и ``update()`` из кода идут мимо ``clean()``.
Писателей slug существующего салона вне тестов сегодня нет.
"""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.urls import reverse

from tenants.models import Tenant
from users.models import User

pytestmark = pytest.mark.django_db

CHANGE = "admin:tenants_tenant_change"
ADD = "admin:tenants_tenant_add"


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="slug-immutable", name="Салон со slug")


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(
        username="slug-owner", password="pw",  # pragma: allowlist secret
        email="slug-owner@example.com", role="admin",
    )


def _client(owner) -> Client:
    c = Client()
    c.force_login(owner)
    return c


def _post_data_from(response) -> dict:
    """Тело POST, какое отправил бы браузер со страницы формы без правок.

    Собирается из контекста GET: основная форма, management-формы инлайнов
    и их строки. Значение берётся тем же виджетом, который его рисует.
    """
    from django import forms

    data: dict[str, str] = {}

    def put(form) -> None:
        for bf in form:
            widget = bf.field.widget
            value = bf.value()
            name = bf.html_name
            if isinstance(widget, forms.CheckboxInput):
                if value:
                    data[name] = "on"
                continue
            inner = getattr(widget, "widget", widget)  # RelatedFieldWidgetWrapper
            if isinstance(inner, forms.MultiWidget):
                parts = value if isinstance(value, (list, tuple)) else inner.decompress(value)
                for i, part in enumerate(parts):
                    data[f"{name}_{i}"] = "" if part is None else str(part)
                continue
            formatted = inner.format_value(value)
            if isinstance(formatted, (list, tuple)):
                formatted = formatted[0] if formatted else ""
            data[name] = "" if formatted is None else str(formatted)

    put(response.context["adminform"].form)
    for inline in response.context["inline_admin_formsets"]:
        put(inline.formset.management_form)
        for form in inline.formset.forms:
            put(form)
    return data


# ---------------------------------------------------------------------------
# Админка
# ---------------------------------------------------------------------------


class TestTheAdmin:
    def test_the_change_form_does_not_offer_slug_for_editing(self, owner, salon):
        page = _client(owner).get(reverse(CHANGE, args=[salon.pk]))
        assert page.status_code == 200, page.status_code
        assert "slug" not in page.context["adminform"].form.fields
        assert salon.slug in page.content.decode("utf-8")

    def test_the_add_form_still_takes_a_slug(self, owner):
        page = _client(owner).get(reverse(ADD))
        assert page.status_code == 200, page.status_code
        assert "slug" in page.context["adminform"].form.fields

    def test_posting_another_slug_on_the_change_form_does_not_rename(self, owner, salon):
        client = _client(owner)
        url = reverse(CHANGE, args=[salon.pk])
        data = _post_data_from(client.get(url))
        data["slug"] = "renamed-salon"
        r = client.post(url, data)
        assert r.status_code == 302, (
            r.context["adminform"].form.errors if r.context else r.status_code
        )
        assert Tenant.all_objects.get(pk=salon.pk).slug == "slug-immutable"

    def test_other_edits_on_the_change_form_still_save(self, owner, salon):
        # Положительная стража: сборщик тела рабочий, и правило не отказывает
        # каждой правке салона.
        client = _client(owner)
        url = reverse(CHANGE, args=[salon.pk])
        data = _post_data_from(client.get(url))
        data["name"] = "Салон переименован"
        r = client.post(url, data)
        assert r.status_code == 302, (
            r.context["adminform"].form.errors if r.context else r.status_code
        )
        stored = Tenant.all_objects.get(pk=salon.pk)
        assert stored.name == "Салон переименован"
        assert stored.slug == "slug-immutable"


# ---------------------------------------------------------------------------
# Модель — любой путь через full_clean()
# ---------------------------------------------------------------------------


class TestTheModel:
    def test_changing_the_slug_of_an_existing_salon_is_refused(self, salon):
        salon.slug = "renamed-salon"
        with pytest.raises(ValidationError) as exc:
            salon.full_clean()
        assert "slug" in exc.value.message_dict
        assert Tenant.all_objects.get(pk=salon.pk).slug == "slug-immutable"

    def test_an_existing_salon_with_the_same_slug_validates(self, salon):
        salon.name = "Другое название"
        salon.full_clean()

    def test_a_new_salon_validates_with_its_slug(self, db):
        Tenant(slug="brand-new-salon", name="Новый салон").full_clean()
