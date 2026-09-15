"""Мастер и услуга ребра — из одного салона (решение владельца, раздел Q; аудит 15.09 §3 п.11).

Ребро ``SpecialistService`` — бронируемая единица: мастер оказывает
услугу салона. До правки модель сверяла только длительность, а тенант ребра
молча брала у услуги (``save()``). Салонный инлайн и отдельная форма
принимали мастера любого салона по raw id. Ребро «мастер салона Б × услуга
салона А» уходило в каталог, слоты и запись салона А.

Правило живёт в ``SpecialistService.clean()``, а ``save()`` его уже зовёт.
Одно место закрывает все пути записи: админку (инлайн и форма),
``update_or_create`` приёма (intake), выбор услуг мастером
(``offer_selection``) и сиды.

* салон мастера читается из базы, а не с закешированного объекта: вызывающий
  мог держать экземпляр профиля до переезда мастера;
* мастер без салона ребро салона не держит;
* собственный тенант ребра, если задан, равен салону услуги;
* ошибка формы, а не 500: ошибка висит на поле, которое в форме есть
  (``specialist``), или не привязана к полю. У инлайна нет поля ``tenant``,
  а ``ModelForm`` превращает ошибку модели на отсутствующем поле в
  ``ValueError``.

Названный предел — как у ``SpecialistProfile.clean`` для ``works_at``:
``bulk_create``/``update()`` идут мимо ``clean()``. ``CheckConstraint``
в соседнюю таблицу не смотрит.

Замер пилота 15.09 17:46 UTC: 524 ребра в 10 салонах, межсалонных пар 0.
Это наблюдение, не гарантия; сторож — этот файл.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory
from django.urls import reverse

from services.models import SalonService, ServiceCategory, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


def _master(tenant, *, username: str, phone: str) -> SpecialistProfile:
    user = User.objects.create_user(
        username=username, password="x",  # pragma: allowlist secret
        role="specialist", phone=phone,
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = username
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


@pytest.fixture
def salon_a(db):
    return Tenant.objects.create(slug="same-tenant-a", name="Салон А")


@pytest.fixture
def salon_b(db):
    return Tenant.objects.create(slug="same-tenant-b", name="Салон Б")


@pytest.fixture
def master_a(salon_a):
    return _master(salon_a, username="same-tenant-master-a", phone="+79991976001")


@pytest.fixture
def master_b(salon_b):
    return _master(salon_b, username="same-tenant-master-b", phone="+79991976002")


@pytest.fixture
def service_a(salon_a):
    category = ServiceCategory.objects.create(name="Same tenant cat", slug="same-tenant-cat")
    return SalonService.objects.create(
        tenant=salon_a, category=category, name="Массаж спины (same tenant)",
        duration_minutes=60, is_active=True,
    )


def _edge(service, master, **kw) -> SpecialistService:
    return SpecialistService.objects.create(
        salon_service=service, specialist=master,
        price=Decimal("1500.00"), duration_minutes=60, **kw,
    )


# ---------------------------------------------------------------------------
# Модель — все пути записи идут через save() → clean()
# ---------------------------------------------------------------------------


class TestTheModelRule:
    def test_a_master_of_the_same_salon_saves_and_the_edge_takes_the_salon(self, service_a, master_a, salon_a):
        edge = _edge(service_a, master_a)
        assert SpecialistService.objects.get(pk=edge.pk).tenant_id == salon_a.id

    def test_a_master_of_another_salon_is_refused(self, service_a, master_b):
        with pytest.raises(ValidationError) as exc:
            _edge(service_a, master_b)
        assert "specialist" in exc.value.message_dict
        assert not SpecialistService.objects.filter(specialist=master_b).exists()

    def test_the_intake_shape_update_or_create_is_refused_for_another_salon(self, service_a, master_b, salon_a):
        with pytest.raises(ValidationError):
            SpecialistService.objects.update_or_create(
                specialist=master_b, salon_service=service_a,
                defaults={"tenant": salon_a, "price": Decimal("1500.00"), "duration_minutes": 60, "is_active": True},
            )
        assert not SpecialistService.objects.filter(specialist=master_b).exists()

    def test_a_master_without_a_salon_is_refused(self, service_a, master_a):
        # Мимо сигнала тестового тенанта по умолчанию: он штампует салон только на save().
        SpecialistProfile.objects.filter(pk=master_a.pk).update(tenant=None)
        with pytest.raises(ValidationError) as exc:
            _edge(service_a, SpecialistProfile.objects.get(pk=master_a.pk))
        assert "specialist" in exc.value.message_dict

    def test_an_edge_tenant_of_another_salon_is_refused(self, service_a, master_a, salon_b):
        with pytest.raises(ValidationError):
            _edge(service_a, master_a, tenant=salon_b)
        assert not SpecialistService.objects.filter(specialist=master_a).exists()

    def test_a_stale_cached_master_does_not_decide(self, service_a, master_a, salon_b):
        # Экземпляр держит салон А, в базе мастер уже переехал в салон Б.
        SpecialistProfile.objects.filter(pk=master_a.pk).update(tenant=salon_b)
        assert master_a.tenant_id != salon_b.id
        with pytest.raises(ValidationError):
            _edge(service_a, master_a)

    def test_moving_an_edge_to_another_salons_master_is_refused(self, service_a, master_a, master_b):
        edge = _edge(service_a, master_a)
        edge.specialist = master_b
        with pytest.raises(ValidationError):
            edge.save()
        assert SpecialistService.objects.get(pk=edge.pk).specialist_id == master_a.id


# ---------------------------------------------------------------------------
# Админка — ошибка формы, а не сохранение и не 500
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(
        username="same-tenant-owner", password="pw",  # pragma: allowlist secret
        email="same-tenant@example.com", role="admin",
    )


def _admin_client(owner) -> Client:
    c = Client()
    c.force_login(owner)
    return c


def _add_body(service, master) -> dict:
    return {
        "salon_service": str(service.pk), "specialist": str(master.pk),
        "price": "1500.00", "duration_minutes": "60", "buffer_after_minutes": "0",
        "is_active": "on",
    }


class TestTheSpecialistServiceAdmin:
    URL = "admin:services_specialistservice_add"

    def test_another_salons_master_is_a_form_error(self, owner, service_a, master_b):
        r = _admin_client(owner).post(reverse(self.URL), _add_body(service_a, master_b))
        assert r.status_code == 200, r.status_code
        assert "specialist" in r.context["adminform"].form.errors
        assert not SpecialistService.objects.filter(specialist=master_b).exists()

    def test_the_same_salons_master_saves(self, owner, service_a, master_a):
        r = _admin_client(owner).post(reverse(self.URL), _add_body(service_a, master_a))
        assert r.status_code == 302, r.context["adminform"].form.errors if r.context else r.status_code
        assert SpecialistService.objects.filter(specialist=master_a, salon_service=service_a).exists()


def _inline_formset(owner, service, rows: list[dict], *, initial: int = 0):
    from services.admin import SpecialistServiceInline

    request = RequestFactory().post("/")
    request.user = owner
    inline = SpecialistServiceInline(SalonService, admin.site)
    FormSet = inline.get_formset(request, service)
    prefix = FormSet.get_default_prefix()
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(rows)),
        f"{prefix}-INITIAL_FORMS": str(initial),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(rows):
        for key, value in row.items():
            data[f"{prefix}-{i}-{key}"] = value
    return FormSet(data, instance=service, prefix=prefix)


def _inline_row(master, **extra) -> dict:
    row = {
        "specialist": str(master.pk), "price": "1500.00", "duration_minutes": "60",
        "buffer_after_minutes": "0", "is_active": "on",
    }
    row.update(extra)
    return row


class TestTheSalonServiceInline:
    def test_another_salons_master_is_a_form_error(self, owner, service_a, master_b):
        formset = _inline_formset(owner, service_a, [_inline_row(master_b)])
        assert not formset.is_valid()
        assert "specialist" in formset.forms[0].errors
        assert not SpecialistService.objects.filter(specialist=master_b).exists()

    def test_the_same_salons_master_saves(self, owner, service_a, master_a):
        formset = _inline_formset(owner, service_a, [_inline_row(master_a)])
        assert formset.is_valid(), formset.errors
        formset.save()
        assert SpecialistService.objects.filter(specialist=master_a, salon_service=service_a).exists()

    def test_editing_a_planted_mismatch_is_a_form_error_not_a_crash(self, owner, service_a, master_a, salon_b):
        edge = _edge(service_a, master_a)
        # Посажено мимо clean() — ровно так, как его обходит update().
        SpecialistService.objects.filter(pk=edge.pk).update(tenant=salon_b)
        formset = _inline_formset(
            owner, service_a, [_inline_row(master_a, id=str(edge.pk), price="1600.00")], initial=1,
        )
        assert not formset.is_valid()
        assert formset.forms[0].non_field_errors() or formset.forms[0].errors
        assert SpecialistService.objects.get(pk=edge.pk).price == Decimal("1500.00")


# ---------------------------------------------------------------------------
# Настоящий HTTP POST форм админки: 200 и ошибка формы, не 500 и не сохранение
# ---------------------------------------------------------------------------
#
# Уровень формсета выше доказывает правило; этот уровень доказывает, что
# админка его показывает. Представление change_view рендерит ошибки, а
# ошибка модели на поле, которого в форме нет, стала бы ValueError, то есть 500.
#
# Достижимость. Новая строка инлайна чужой tenant получить не может по
# построению: поля ``tenant`` в инлайне нет, а ``save()`` берёт его у услуги-
# родителя. Несовпадение tenant в инлайне бывает только на строке, посаженной
# мимо ``clean()`` (``update``/``bulk_create``, старые данные), — такой тест ниже.
# В отдельной форме ребра ``tenant`` есть, и там несовпадение достижимо напрямую.


def _post_data_from(response) -> dict:
    """Тело POST, какое отправил бы браузер со страницы формы без правок.

    Собирается из контекста GET: основная форма, management-формы инлайнов
    и их строки. Значение берётся тем же виджетом, который его рисует, —
    иначе тест проверял бы собственный сборщик, а не админку.
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


def _edges_formset(response):
    for inline in response.context["inline_admin_formsets"]:
        if inline.formset.model is SpecialistService:
            return inline.formset
    raise AssertionError("SpecialistServiceInline is not on the salon service form")


class TestTheAdminOverHttp:
    ADD = "admin:services_specialistservice_add"
    CHANGE = "admin:services_salonservice_change"

    def test_edge_form_with_a_foreign_edge_tenant_is_200_and_a_form_error(
        self, owner, service_a, master_a, salon_b,
    ):
        body = _add_body(service_a, master_a) | {"tenant": str(salon_b.pk)}
        r = _admin_client(owner).post(reverse(self.ADD), body)
        assert r.status_code == 200, r.status_code
        form = r.context["adminform"].form
        assert form.non_field_errors(), form.errors
        assert not SpecialistService.objects.filter(specialist=master_a).exists()

    @staticmethod
    def _with_new_row(client, service, master) -> tuple[dict, str, int]:
        url = reverse(TestTheAdminOverHttp.CHANGE, args=[service.pk])
        page = client.get(url)
        assert page.status_code == 200, page.status_code
        data = _post_data_from(page)
        formset = _edges_formset(page)
        prefix, index = formset.prefix, int(data[f"{formset.prefix}-TOTAL_FORMS"])
        data[f"{prefix}-TOTAL_FORMS"] = str(index + 1)
        data.update({
            f"{prefix}-{index}-specialist": str(master.pk),
            f"{prefix}-{index}-price": "1500.00",
            f"{prefix}-{index}-duration_minutes": "60",
            f"{prefix}-{index}-buffer_after_minutes": "0",
            f"{prefix}-{index}-is_active": "on",
            f"{prefix}-{index}-salon_service": str(service.pk),
        })
        return data, url, index

    def test_the_form_builder_saves_an_unchanged_page_and_a_same_salon_row(self, owner, service_a, master_a):
        # Положительная стража: без неё отказы ниже могли бы «пройти» на
        # сломанном сборщике тела, который админка отвергает по другой причине.
        client = _admin_client(owner)
        data, url, _ = self._with_new_row(client, service_a, master_a)
        r = client.post(url, data)
        assert r.status_code == 302, (
            r.context["adminform"].form.errors, _edges_formset(r).errors,
        ) if r.context else r.status_code
        assert SpecialistService.objects.filter(specialist=master_a, salon_service=service_a).exists()

    def test_inline_row_with_another_salons_master_is_200_and_a_form_error(self, owner, service_a, master_b):
        client = _admin_client(owner)
        data, url, index = self._with_new_row(client, service_a, master_b)
        r = client.post(url, data)
        assert r.status_code == 200, r.status_code
        assert r.context["adminform"].form.errors == {}, r.context["adminform"].form.errors
        assert "specialist" in _edges_formset(r).forms[index].errors
        assert not SpecialistService.objects.filter(specialist=master_b).exists()

    def test_inline_edit_of_a_planted_foreign_tenant_is_200_and_a_form_error(
        self, owner, service_a, master_a, salon_b,
    ):
        edge = _edge(service_a, master_a)
        # Достижимо только мимо clean(): update() — тот самый обход.
        SpecialistService.objects.filter(pk=edge.pk).update(tenant=salon_b)
        client = _admin_client(owner)
        url = reverse(self.CHANGE, args=[service_a.pk])
        page = client.get(url)
        assert page.status_code == 200, page.status_code
        data = _post_data_from(page)
        formset = _edges_formset(page)
        row = next(i for i, f in enumerate(formset.forms) if f.instance.pk == edge.pk)
        data[f"{formset.prefix}-{row}-price"] = "1600.00"
        r = client.post(url, data)
        assert r.status_code == 200, r.status_code
        assert r.context["adminform"].form.errors == {}, r.context["adminform"].form.errors
        assert _edges_formset(r).forms[row].non_field_errors(), _edges_formset(r).errors
        assert SpecialistService.objects.get(pk=edge.pk).price == Decimal("1500.00")
