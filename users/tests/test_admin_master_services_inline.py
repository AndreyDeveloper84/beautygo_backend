"""Услуги мастера правятся на карточке мастера.

Владелец 09.11.2026 включил мастеру услугу и пошёл проверять привязку на
карточке мастера. Привязки там не было: предложение мастера правилось с двух
экранов, и оба про услугу, а не про человека — строка в блоке на форме услуги
салона и отдельный список, где мастера ищут строкой поиска.

Что эти тесты держат помимо «блок смонтирован»:

* **в списке новой строки — только услуги салона мастера, которых у него ещё
  нет.** Чужую услугу выбрать нечем, а подставленная в запрос — ошибка формы,
  не сохранение;
* **услуга существующей строки не меняется.** Идентификатор строки — ключ, по
  которому бот находит запись; смена услуги увела бы его молча. Значение
  берётся из базы, а не из запроса;
* **строку не удаляют, а выключают.** Удаление рвёт тот же ключ;
* **ошибка — это ошибка формы, а не страница с ошибкой сервера.** Одна услуга
  дважды в одной отправке упиралась бы в ограничение базы, а строка без
  услуги и без длительности — в чтение услуги, которой нет.
"""
from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from django import forms
from django.contrib import admin as django_admin
from django.test import Client, RequestFactory
from django.urls import reverse

from services.admin import MasterServicesInline
from services.models import SalonService, ServiceCategory, SpecialistService
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db


# ─── стенд ──────────────────────────────────────────────────────────────────


def _salon(name: str) -> Tenant:
    return Tenant.objects.create(name=name, slug=f"msi-{uuid4().hex[:8]}")


def _master(tenant: Tenant, name: str) -> SpecialistProfile:
    user = User.objects.create_user(
        username=f"msi-{uuid4().hex[:8]}",
        password="pw",  # pragma: allowlist secret
        role="specialist",
    )
    # Профиль заводится сигналом при создании пользователя с ролью мастера.
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = name
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.save()
    return profile


def _service(tenant: Tenant, name: str, *, is_active: bool = True) -> SalonService:
    category = ServiceCategory.objects.create(name=f"Категория {name}", slug=f"msi-{uuid4().hex[:8]}")
    return SalonService.objects.create(
        tenant=tenant, category=category, name=name, duration_minutes=60, is_active=is_active,
    )


def _edge(service: SalonService, master: SpecialistProfile, **kw) -> SpecialistService:
    return SpecialistService.objects.create(
        salon_service=service, specialist=master,
        price=Decimal("1500.00"), duration_minutes=60, **kw,
    )


@pytest.fixture
def salon() -> Tenant:
    return _salon("Салон мастера")


@pytest.fixture
def other_salon() -> Tenant:
    return _salon("Чужой салон")


@pytest.fixture
def master(salon: Tenant) -> SpecialistProfile:
    return _master(salon, "Мастер блока")


@pytest.fixture
def operator() -> User:
    return User.objects.create_superuser(
        username=f"msi-operator-{uuid4().hex[:6]}",
        password="pw",  # pragma: allowlist secret
        email="msi-operator@example.com",
        role="admin",
    )


def _inline() -> MasterServicesInline:
    return MasterServicesInline(SpecialistProfile, django_admin.site)


def _request(operator: User, method: str = "post"):
    request = getattr(RequestFactory(), method)("/")
    request.user = operator
    return request


def _formset(operator: User, master: SpecialistProfile, rows: list[dict], *, initial: int = 0):
    FormSet = _inline().get_formset(_request(operator), master)
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
    return FormSet(data, instance=master, prefix=prefix)


def _row(service: SalonService | None = None, **extra) -> dict:
    row = {"price": "1500.00", "duration_minutes": "60", "buffer_after_minutes": "0", "is_active": "on"}
    if service is not None:
        row["salon_service"] = str(service.pk)
    row.update(extra)
    return row


def _offered(operator: User, master: SpecialistProfile) -> set[str]:
    """Названия услуг в списке новой строки."""
    FormSet = _inline().get_formset(_request(operator, "get"), master)
    empty = FormSet(instance=master).empty_form
    return {label for value, label in empty.fields["salon_service"].choices if value}


# ─── блок на форме мастера ──────────────────────────────────────────────────


def test_the_services_live_on_the_master_form():
    model_admin = django_admin.site._registry[SpecialistProfile]
    assert MasterServicesInline in model_admin.inlines


def test_the_block_offers_no_empty_rows_and_no_delete_column():
    """Пустая строка — приглашение завести предложение, которого нет.
    Удаление рвёт ключ записи: строку выключают флажком."""
    assert MasterServicesInline.extra == 0
    assert MasterServicesInline.can_delete is False


# ─── список новой строки ────────────────────────────────────────────────────


def test_a_new_row_offers_the_salons_services_the_master_does_not_have_yet(
    operator, master, salon, other_salon,
):
    free = _service(salon, "Свободная услуга")
    taken = _service(salon, "Уже у мастера")
    foreign = _service(other_salon, "Чужая услуга")
    _edge(taken, master)

    offered = _offered(operator, master)

    assert free.name in offered
    assert taken.name not in offered
    assert foreign.name not in offered


def test_a_service_another_master_has_is_still_offered(operator, master, salon):
    """«Уже есть» считается по этому мастеру, а не по салону."""
    shared = _service(salon, "Общая услуга")
    _edge(shared, _master(salon, "Коллега"))

    assert shared.name in _offered(operator, master)


def test_a_switched_off_service_is_offered_and_says_so(operator, master, salon):
    _service(salon, "Сезонная", is_active=False)

    assert "Сезонная (услуга выключена)" in _offered(operator, master)


# ─── новая строка ───────────────────────────────────────────────────────────


def test_a_new_row_saves_and_takes_the_salon(operator, master, salon):
    service = _service(salon, "Новая услуга мастера")

    formset = _formset(operator, master, [_row(service)])

    assert formset.is_valid(), (formset.errors, formset.non_form_errors())
    formset.save()
    edge = SpecialistService.objects.get(specialist=master, salon_service=service)
    assert edge.tenant_id == salon.id
    assert edge.is_active is True


def test_another_salons_service_is_a_form_error(operator, master, salon, other_salon):
    own = _service(salon, "Своя услуга")
    foreign = _service(other_salon, "Услуга чужого салона")

    accepted = _formset(operator, master, [_row(own)])
    refused = _formset(operator, master, [_row(foreign)])

    assert accepted.is_valid(), accepted.errors
    accepted.save()
    assert not refused.is_valid()
    assert "salon_service" in refused.forms[0].errors
    bound = set(SpecialistService.objects.filter(specialist=master).values_list("salon_service_id", flat=True))
    assert bound == {own.id}


def test_the_same_service_twice_in_one_submit_is_a_form_error(operator, master, salon):
    """Каждая строка по отдельности проходит: пары в базе ещё нет. Вторая
    упала бы на сохранении, на ограничении базы."""
    service = _service(salon, "Двойная услуга")
    other = _service(salon, "Вторая услуга")

    twice = _formset(operator, master, [_row(service), _row(service)])
    different = _formset(operator, master, [_row(service), _row(other)])

    assert not twice.is_valid()
    assert any("добавлена дважды" in error for error in twice.non_form_errors())
    assert different.is_valid(), (different.errors, different.non_form_errors())


def test_a_master_without_a_salon_gets_no_new_rows(operator, master):
    with_salon = _inline().has_add_permission(_request(operator, "get"), master)
    # Мимо сигнала тестового салона по умолчанию: он штампует салон на save().
    SpecialistProfile.objects.filter(pk=master.pk).update(tenant=None)
    homeless = SpecialistProfile.objects.get(pk=master.pk)

    assert with_salon is True
    assert _inline().has_add_permission(_request(operator, "get"), homeless) is False
    assert _inline().has_add_permission(_request(operator, "get"), None) is False


def test_a_forged_row_for_a_master_without_a_salon_saves_nothing(operator, master, salon):
    """Кнопки «добавить» у такого мастера нет; строка, присланная мимо
    кнопки, не сохраняется. Та же строка мастеру с салоном — сохраняется."""
    service = _service(salon, "Услуга для обоих")
    homeless = _master(salon, "Без салона")
    SpecialistProfile.objects.filter(pk=homeless.pk).update(tenant=None)
    homeless = SpecialistProfile.objects.get(pk=homeless.pk)

    for owner in (master, homeless):
        formset = _formset(operator, owner, [_row(service)])
        if formset.is_valid():
            formset.save()

    assert SpecialistService.objects.filter(specialist=master, salon_service=service).count() == 1
    assert SpecialistService.objects.filter(specialist=homeless).count() == 0


# ─── существующая строка ────────────────────────────────────────────────────


def test_an_existing_row_is_switched_off_in_place(operator, master, salon):
    edge = _edge(_service(salon, "Выключаемая"), master)
    row = _row(id=str(edge.pk), price="1700.00")
    del row["is_active"]

    formset = _formset(operator, master, [row], initial=1)

    assert formset.is_valid(), (formset.errors, formset.non_form_errors())
    formset.save()
    edge.refresh_from_db()
    assert edge.is_active is False
    assert edge.price == Decimal("1700.00")


def test_the_service_of_an_existing_row_does_not_change(operator, master, salon):
    """Идентификатор строки — ключ записи. Услуга берётся из базы, а не из
    запроса: подставленная в POST другая услуга ничего не меняет."""
    original = _service(salon, "Исходная услуга")
    another = _service(salon, "Другая услуга")
    edge = _edge(original, master)

    formset = _formset(
        operator, master, [_row(another, id=str(edge.pk), price="1800.00")], initial=1,
    )

    assert formset.is_valid(), (formset.errors, formset.non_form_errors())
    formset.save()
    edge.refresh_from_db()
    assert edge.price == Decimal("1800.00")
    assert edge.salon_service_id == original.id


def test_a_delete_flag_in_the_request_keeps_the_row(operator, master, salon):
    """Колонки «Удалить» нет; флажок, присланный мимо неё, строку не удаляет.
    Остальные поля той же строки при этом сохраняются."""
    edge = _edge(_service(salon, "Неудаляемая"), master)

    formset = _formset(
        operator, master, [_row(id=str(edge.pk), price="1900.00", DELETE="on")], initial=1,
    )

    assert formset.is_valid(), (formset.errors, formset.non_form_errors())
    formset.save()
    edge.refresh_from_db()
    assert edge.price == Decimal("1900.00")


def test_a_colleagues_row_is_out_of_reach(operator, master, salon):
    """Блок правит строки своего мастера. Идентификатор чужой строки,
    подставленный в запрос, её не меняет; свою — меняет."""
    service = _service(salon, "Услуга двоих")
    mine = _edge(service, master)
    theirs = _edge(service, _master(salon, "Коллега"))

    for edge in (mine, theirs):
        formset = _formset(operator, master, [_row(id=str(edge.pk), price="2100.00")], initial=1)
        if formset.is_valid():
            formset.save()

    mine.refresh_from_db()
    theirs.refresh_from_db()
    assert mine.price == Decimal("2100.00")
    assert theirs.price == Decimal("1500.00")


# ─── настоящая страница ─────────────────────────────────────────────────────


def _page_data(page) -> dict:
    """Тело POST, какое отправил бы браузер со страницы без правок."""
    data: dict[str, str] = {}

    def put(form) -> None:
        for bound in form:
            value = bound.value()
            if isinstance(bound.field.widget, forms.CheckboxInput):
                if value:
                    data[bound.html_name] = "on"
                continue
            data[bound.html_name] = "" if value is None else str(getattr(value, "pk", value))

    put(page.context["adminform"].form)
    for inline in page.context["inline_admin_formsets"]:
        put(inline.formset.management_form)
        for form in inline.formset.forms:
            put(form)
    return data


def _services_block(page):
    return next(
        inline.formset for inline in page.context["inline_admin_formsets"]
        if inline.formset.model is SpecialistService
    )


def _open(operator: User, master: SpecialistProfile):
    client = Client()
    client.force_login(operator)
    url = reverse("admin:users_specialistprofile_change", args=[master.pk])
    page = client.get(url)
    assert page.status_code == 200, page.status_code
    return client, url, page


def test_the_page_shows_this_masters_rows_and_only_them(operator, master, salon):
    mine = _edge(_service(salon, "Услуга этого мастера"), master)
    theirs = _edge(_service(salon, "Услуга коллеги"), _master(salon, "Коллега"))

    _client, _url, page = _open(operator, master)

    shown = {form.instance.pk for form in _services_block(page).initial_forms}
    assert mine.pk in shown
    assert theirs.pk not in shown


def test_the_page_lists_only_the_salons_free_services_for_a_new_row(
    operator, master, salon, other_salon,
):
    """То же, что проверено на поле формы, — на отрисованной странице."""
    _service(salon, "Свободная услуга страницы")
    _service(other_salon, "Чужая услуга страницы")

    _client, _url, page = _open(operator, master)
    html = page.content.decode()

    assert "Свободная услуга страницы" in html
    assert "Чужая услуга страницы" not in html


def test_a_row_from_another_salon_is_a_visible_row_error(operator, master, salon, other_salon):
    """Строка, посаженная мимо ``clean()``: услуга оказалась в другом салоне.
    Модель вешает ошибку на ``specialist`` — здесь это скрытое поле, и без
    переноса страница говорила бы «исправьте ошибки» и не показывала ни одной."""
    service = _service(salon, "Уехавшая услуга")
    _edge(service, master)
    client, url, page = _open(operator, master)
    before = client.post(url, _page_data(page))

    SalonService.objects.filter(pk=service.pk).update(tenant=other_salon)
    client, url, page = _open(operator, master)
    after = client.post(url, _page_data(page))

    assert before.status_code == 302, before.status_code
    assert after.status_code == 200, after.status_code
    row = _services_block(after).forms[0]
    assert any("не из салона" in error for error in row.non_field_errors())


def test_an_unchanged_page_saves_and_a_switched_off_row_stays_off(operator, master, salon):
    kept = _edge(_service(salon, "Остаётся включённой"), master)
    dropped = _edge(_service(salon, "Выключается"), master)
    client, url, page = _open(operator, master)
    data = _page_data(page)
    block = _services_block(page)
    index = next(i for i, form in enumerate(block.forms) if form.instance.pk == dropped.pk)
    del data[f"{block.prefix}-{index}-is_active"]

    response = client.post(url, data)

    assert response.status_code == 302, (
        response.context["adminform"].form.errors if response.context else response.status_code
    )
    kept.refresh_from_db()
    dropped.refresh_from_db()
    assert kept.is_active is True
    assert dropped.is_active is False


def test_a_row_without_a_service_is_a_form_error_not_a_server_error(operator, master, salon):
    """Строка добавлена, услуга не выбрана, длительность пуста. Модель искала
    длительность у услуги, которой нет, и отвечала исключением, а не ошибкой
    формы."""
    _service(salon, "Есть что выбрать")
    client, url, page = _open(operator, master)
    data = _page_data(page)
    block = _services_block(page)
    index = int(data[f"{block.prefix}-TOTAL_FORMS"])
    data[f"{block.prefix}-TOTAL_FORMS"] = str(index + 1)
    data.update({
        f"{block.prefix}-{index}-price": "1500.00",
        f"{block.prefix}-{index}-buffer_after_minutes": "0",
        f"{block.prefix}-{index}-is_active": "on",
    })

    response = client.post(url, data)

    assert response.status_code == 200, response.status_code
    assert "salon_service" in _services_block(response).forms[index].errors
    assert SpecialistService.objects.filter(specialist=master).count() == 0
