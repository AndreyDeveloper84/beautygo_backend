"""Подтверждение места в админке: без 500 и без выдуманного происхождения (раздел Q; аудит 15.09 §3 п.6).

Решение владельца: не ставить CONFIRMED без ``confirmed_by`` / ``confirmed_at`` /
``confirmed_source_ref``.

Что было:
* инлайн мест на форме салона показывал ``status``, но не три поля
  подтверждения. Выбор «confirmed» — это ошибки модели на полях, которых в форме
  нет, то есть ``ValueError`` и 500;
* ту же 500 давала любая правка в инлайне строки, посаженной мимо ``clean()`` с
  «геокодировано, но без координат». Поле ``geocode_status`` в инлайне только
  для чтения, а схема такую строку не запрещает;
* действия «подтвердить (я)» не было. В отдельной форме ``confirmed_by`` вводился
  как UUID, ``confirmed_at`` — руками.

Правило:
* в инлайне «confirmed» не предлагается месту, которое ещё не подтверждено
  с происхождением. Попытка — ошибка на ``status`` (200), место не меняется;
* ошибка модели на поле, которого нет в форме, не теряется и не роняет форму:
  её текст виден оператору дословно, строка не сохраняется;
* действие «Подтвердить место (я)»:
  - кто — нажавший, когда — сейчас;
  - основание называет человек на промежуточной странице, поле обязательно,
    умолчания нет, пробелы — не основание;
  - результат называется по каждому месту, частичный успех — тоже.
"""
from __future__ import annotations

from datetime import datetime, timezone as dt_tz

import pytest
from django.contrib.messages import get_messages
from django.test import Client
from django.urls import reverse

from tenants.models import GeocodeStatus, LocationStatus, ServiceLocation, Tenant
from users.models import User

pytestmark = pytest.mark.django_db

TENANT_CHANGE = "admin:tenants_tenant_change"
PLACES = "admin:tenants_servicelocation_changelist"
PLACE_CHANGE = "admin:tenants_servicelocation_change"
ACTION = "confirm_location_as_me"
BASIS = "договор с салоном от 15.09"
NO_COORDS_TEXT = "означает успешное геокодирование, но координат нет"


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="place-confirm-salon", name="Салон мест")


@pytest.fixture
def operator(db):
    return User.objects.create_superuser(
        username="place-operator", password="pw",  # pragma: allowlist secret
        email="place-operator@example.com", role="admin",
    )


@pytest.fixture
def other_confirmer(db):
    return User.objects.create_user(
        username="place-other", password="x",  # pragma: allowlist secret
        phone="+79991975002",
    )


def _client(operator) -> Client:
    c = Client(raise_request_exception=False)
    c.force_login(operator)
    return c


def _place(salon, label: str, **kw) -> ServiceLocation:
    return ServiceLocation.objects.create(
        tenant=salon, label=label, address=f"ул. Тестовая, {label}", city="Пенза", **kw,
    )


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


def _places_formset(response):
    for inline in response.context["inline_admin_formsets"]:
        if inline.formset.model is ServiceLocation:
            return inline.formset
    raise AssertionError("ServiceLocationInline is not on the salon form")


def _row_of(formset, place) -> int:
    return next(i for i, f in enumerate(formset.forms) if f.instance.pk == place.pk)


def _messages(response) -> list[str]:
    return [str(m) for m in get_messages(response.wsgi_request)]


# ---------------------------------------------------------------------------
# Инлайн мест на форме салона
# ---------------------------------------------------------------------------


class TestTheInlineOnTheSalon:
    def test_choosing_confirmed_in_the_inline_is_a_status_error_not_a_500(self, operator, salon):
        place = _place(salon, "Место инлайна")
        client = _client(operator)
        url = reverse(TENANT_CHANGE, args=[salon.pk])
        page = client.get(url)
        assert page.status_code == 200, page.status_code
        data = _post_data_from(page)
        formset = _places_formset(page)
        row = _row_of(formset, place)
        data[f"{formset.prefix}-{row}-status"] = LocationStatus.CONFIRMED

        r = client.post(url, data)
        assert r.status_code == 200, r.status_code
        assert "status" in _places_formset(r).forms[row].errors
        place.refresh_from_db()
        assert place.status == LocationStatus.REVIEW_REQUIRED
        assert place.confirmed_by_id is None

    def test_editing_a_planted_geocoded_row_without_coordinates_shows_the_model_text(self, operator, salon):
        place = _place(salon, "Место без координат")
        # Посажено мимо clean(): схема такую строку не запрещает.
        ServiceLocation.objects.filter(pk=place.pk).update(geocode_status=GeocodeStatus.OK)
        client = _client(operator)
        url = reverse(TENANT_CHANGE, args=[salon.pk])
        page = client.get(url)
        assert page.status_code == 200, page.status_code
        data = _post_data_from(page)
        formset = _places_formset(page)
        row = _row_of(formset, place)
        data[f"{formset.prefix}-{row}-label"] = "Переименовано"

        r = client.post(url, data)
        assert r.status_code == 200, r.status_code
        assert NO_COORDS_TEXT in r.content.decode("utf-8"), "the model error must be shown verbatim"
        place.refresh_from_db()
        assert place.label == "Место без координат"

    def test_an_ordinary_label_edit_in_the_inline_still_saves(self, operator, salon):
        place = _place(salon, "Обычное место")
        client = _client(operator)
        url = reverse(TENANT_CHANGE, args=[salon.pk])
        page = client.get(url)
        data = _post_data_from(page)
        formset = _places_formset(page)
        data[f"{formset.prefix}-{_row_of(formset, place)}-label"] = "Обычное место, вход со двора"

        r = client.post(url, data)
        assert r.status_code == 302, r.status_code
        place.refresh_from_db()
        assert place.label == "Обычное место, вход со двора"


# ---------------------------------------------------------------------------
# Действие «Подтвердить место (я)»
# ---------------------------------------------------------------------------


def _act(client, places, **extra):
    body = {"action": ACTION, "index": "0", "_selected_action": [str(p.pk) for p in places]}
    body.update(extra)
    return client.post(reverse(PLACES), body)


class TestTheConfirmAction:
    def test_the_first_step_shows_the_places_and_writes_nothing(self, operator, salon):
        place = _place(salon, "Место на подтверждение")
        r = _act(_client(operator), [place])
        assert r.status_code == 200, r.status_code
        assert "Место на подтверждение" in r.content.decode("utf-8")
        assert 'name="source_ref"' in r.content.decode("utf-8")
        place.refresh_from_db()
        assert place.status == LocationStatus.REVIEW_REQUIRED

    def test_confirming_with_a_named_basis_records_who_when_and_why(self, operator, salon):
        place = _place(salon, "Место с основанием")
        before = datetime.now(tz=dt_tz.utc)
        r = _act(_client(operator), [place], confirm="yes", source_ref=BASIS)
        assert r.status_code == 302, r.status_code
        place.refresh_from_db()
        assert place.status == LocationStatus.CONFIRMED
        assert place.confirmed_by_id == operator.pk
        assert place.confirmed_at is not None and place.confirmed_at >= before
        assert place.confirmed_source_ref == BASIS

    @pytest.mark.parametrize("basis", ["", "   "], ids=["empty", "spaces"])
    def test_an_empty_or_blank_basis_is_refused_and_nothing_is_confirmed(self, operator, salon, basis):
        place = _place(salon, "Место без основания")
        r = _act(_client(operator), [place], confirm="yes", source_ref=basis)
        assert r.status_code == 200, r.status_code
        assert 'name="source_ref"' in r.content.decode("utf-8")
        place.refresh_from_db()
        assert place.status == LocationStatus.REVIEW_REQUIRED
        assert place.confirmed_by_id is None

    def test_several_places_get_a_result_each_and_a_partial_success_is_named(
        self, operator, other_confirmer, salon,
    ):
        fine = _place(salon, "Место годное")
        no_address = _place(salon, "Место без адреса")
        ServiceLocation.objects.filter(pk=no_address.pk).update(address="")
        already = _place(
            salon, "Место уже подтверждено", status=LocationStatus.CONFIRMED,
            confirmed_by=other_confirmer, confirmed_at=datetime(2026, 9, 11, tzinfo=dt_tz.utc),
            confirmed_source_ref="решение владельца §10",
        )
        client = _client(operator)
        r = _act(client, [fine, no_address, already], confirm="yes", source_ref=BASIS)
        assert r.status_code == 302, r.status_code
        text = " | ".join(_messages(r))

        fine.refresh_from_db()
        no_address.refresh_from_db()
        already.refresh_from_db()
        assert fine.status == LocationStatus.CONFIRMED and fine.confirmed_by_id == operator.pk
        assert no_address.status == LocationStatus.REVIEW_REQUIRED
        assert already.confirmed_by_id == other_confirmer.pk
        assert already.confirmed_source_ref == "решение владельца §10"
        for label in ("Место годное", "Место без адреса", "Место уже подтверждено"):
            assert label in text, (label, text)


# ---------------------------------------------------------------------------
# Отдельная форма места — прежний путь не сломан
# ---------------------------------------------------------------------------


class TestThePlaceForm:
    def test_confirming_by_hand_with_full_provenance_still_saves(self, operator, salon):
        place = _place(salon, "Место руками")
        client = _client(operator)
        url = reverse(PLACE_CHANGE, args=[place.pk])
        page = client.get(url)
        assert page.status_code == 200, page.status_code
        data = _post_data_from(page)
        data.update({
            "status": LocationStatus.CONFIRMED,
            "confirmed_by": str(operator.pk),
            "confirmed_at_0": "2026-09-15",
            "confirmed_at_1": "12:00:00",
            "confirmed_source_ref": BASIS,
        })
        r = client.post(url, data)
        assert r.status_code == 302, (
            r.status_code, r.context["adminform"].form.errors if r.context else None,
        )
        place.refresh_from_db()
        assert place.status == LocationStatus.CONFIRMED
        assert place.confirmed_by_id == operator.pk
        assert place.confirmed_source_ref == BASIS
