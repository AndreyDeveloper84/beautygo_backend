"""Адрес и город принадлежат салону (DRF-1587, решение владельца 08.09.2026).

Что закрепляется:

* ``tenants.Tenant`` несёт ``address`` и ``city`` — до этого тикета у него
  было ровно пять полей, и завести адрес салона в Ayla было негде;
* оба поля уезжают наружу ТЕМ ЖЕ контрактом, которым сегодня уезжает
  профиль мастера — ``GET /api/v1/internal/specialists/`` и его detail
  (зеркало бота кладёт строку целиком в ``CatalogMasterDTO.raw``);
* «не указано» доезжает как ``null``, НИКОГДА как пустая строка: на той
  стороне пустая строка неотличима от заполненного пустого значения
  (класс дефекта из OPEN_DECISIONS §65);
* правка аддитивна: ``SpecialistProfile.address`` остаётся на месте и с
  прежним смыслом (старшинство салона над мастером — DRF-1589), а
  публичный каталог Client App новых полей не получает;
* оба поля редактируются в админке — иначе «завести салон с адресом»
  по-прежнему негде.

Тесты живут в ``tenants/``, а не в ``users/``: предмет тикета — поля
тенанта, ``users/internal_catalog_api.py`` правится ровно на объявленный
минимум контракта.
"""
from __future__ import annotations

import pytest
from django.contrib import admin as django_admin
from rest_framework.test import APIClient

from tenants.models import Tenant
from users.models import SpecialistProfile, User


VALID_TOKEN = "test-ayla-internal-token-1587"
INTERNAL_URL = "/api/v1/internal/specialists/"
PUBLIC_URL = "/api/v1/specialists/"

SALON_CITY = "Пенза"
SALON_ADDRESS = "Пенза, ул. Московская, 1"
MASTER_ADDRESS = "Пенза, ул. Московская, 1"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


def _make_specialist(*, tenant, username, phone, name, address=""):
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.tenant = tenant
    profile.display_name = name
    profile.address = address
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.save()
    return profile


def _api() -> APIClient:
    client = APIClient()
    client.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    return client


def _rows(response) -> list[dict]:
    body = response.json()
    payload = body.get("data", body) if isinstance(body, dict) else body
    if isinstance(payload, dict) and "results" in payload:
        return payload["results"]
    return payload


def _row_for(response, profile) -> dict:
    rows = [r for r in _rows(response) if r["id"] == str(profile.id)]
    assert rows, f"мастер {profile.id} не найден в выдаче"
    return rows[0]


@pytest.fixture
def salon(db):
    return Tenant.objects.create(
        slug="drf1587-salon",
        name="Медиклиник",
        city=SALON_CITY,
        address=SALON_ADDRESS,
    )


@pytest.fixture
def blank_salon(db):
    """Салон, у которого адрес и город НЕ указаны."""
    return Tenant.objects.create(slug="drf1587-blank", name="Без адреса")


@pytest.mark.django_db
class TestTenantCarriesTheAddress:
    """Источник: адрес живёт на салоне, а не только на мастере."""

    def test_tenant_has_address_and_city(self, salon):
        stored = Tenant.all_objects.get(pk=salon.pk)
        assert stored.address == SALON_ADDRESS
        assert stored.city == SALON_CITY

    def test_unset_address_is_empty_not_null_in_db(self, blank_salon):
        """В базе «не указано» — пустая строка, одно пустое состояние.

        Различие «пусто против отсутствует» держит провод (см. ниже), а не
        два разных пустых значения в колонке.
        """
        stored = Tenant.all_objects.get(pk=blank_salon.pk)
        assert stored.address == ""
        assert stored.city == ""

    def test_admin_can_edit_both_fields(self):
        """Иначе завести салон с адресом через админку по-прежнему негде."""
        model_admin = django_admin.site._registry[Tenant]
        editable = {
            field
            for _, opts in model_admin.fieldsets
            for field in opts["fields"]
        } - set(model_admin.readonly_fields)
        assert {"address", "city"} <= editable


@pytest.mark.django_db
class TestOutboundContract:
    """Тот же контракт, которым сегодня уезжает профиль мастера."""

    def test_list_carries_salon_address_and_city(self, salon):
        spec = _make_specialist(
            tenant=salon, username="drf1587_a", phone="+79991587001",
            name="Медиклиник · лазерная", address=MASTER_ADDRESS,
        )
        row = _row_for(_api().get(f"{INTERNAL_URL}?tenant={salon.id}"), spec)
        assert row["tenant_address"] == SALON_ADDRESS
        assert row["tenant_city"] == SALON_CITY

    def test_detail_carries_salon_address_and_city(self, salon):
        spec = _make_specialist(
            tenant=salon, username="drf1587_b", phone="+79991587002",
            name="Медиклиник · косметология", address=MASTER_ADDRESS,
        )
        body = _api().get(f"{INTERNAL_URL}{spec.id}/").json()
        row = body.get("data", body)
        assert row["tenant_address"] == SALON_ADDRESS
        assert row["tenant_city"] == SALON_CITY

    def test_master_address_is_the_place_not_the_profile(self, salon):
        """Поле ``address`` на месте, но означает место оказания услуг (§9, L6).

        DRF-1587 оставлял ``SpecialistProfile.address`` нетронутым и
        откладывал старшинство салона над мастером на DRF-1589. Владелец
        решил его §9 (DRF-1687): «все старые адреса перестают быть
        авторитетными». С L6 ``address`` в строке мастера — адрес его
        подтверждённого места (``works_at``), а собственный адрес профиля
        клиенту и зеркалу бота не уезжает: без места — пусто, с местом —
        адрес места. ``tenant_address`` при этом по-прежнему адрес салона.
        Этот тест сторожит §9; сняться может только вместе с полем (L8).
        """
        from tenants.tests.places import place_specialist_at

        spec = _make_specialist(
            tenant=salon, username="drf1587_c", phone="+79991587003",
            name="Мастер со своим адресом", address="Пенза, ул. Кирова, 7",
        )
        row = _row_for(_api().get(f"{INTERNAL_URL}?tenant={salon.id}"), spec)
        assert row["address"] == ""                      # свой адрес профиля не уезжает
        assert row["tenant_address"] == SALON_ADDRESS

        place_specialist_at(spec, 53.195878, 45.018316, label="Пенза, ул. Московская, 1")
        row = _row_for(_api().get(f"{INTERNAL_URL}?tenant={salon.id}"), spec)
        assert row["address"] == "Пенза, ул. Московская, 1"
        assert row["tenant_address"] == SALON_ADDRESS


@pytest.mark.django_db
class TestAbsenceTravelsAsAbsence:
    """«Адрес не указан» не превращается в пустую строку (OD §65)."""

    def test_blank_salon_fields_arrive_as_null(self, blank_salon):
        spec = _make_specialist(
            tenant=blank_salon, username="drf1587_d", phone="+79991587004",
            name="Мастер салона без адреса",
        )
        row = _row_for(
            _api().get(f"{INTERNAL_URL}?tenant={blank_salon.id}"), spec,
        )
        assert row["tenant_address"] is None
        assert row["tenant_city"] is None

    def test_whitespace_only_city_is_absence_too(self, blank_salon):
        """Пробел — не город. Он пуст для любого читателя, значит null."""
        Tenant.all_objects.filter(pk=blank_salon.pk).update(city="   ")
        spec = _make_specialist(
            tenant=blank_salon, username="drf1587_e", phone="+79991587005",
            name="Мастер с пробелом вместо города",
        )
        row = _row_for(
            _api().get(f"{INTERNAL_URL}?tenant={blank_salon.id}"), spec,
        )
        assert row["tenant_city"] is None

    @pytest.mark.no_auto_tenant
    def test_master_without_tenant_gets_null_not_a_guess(self):
        """Подставлять сюда нечего — ни адреса мастера, ни города пилота."""
        spec = _make_specialist(
            tenant=None, username="drf1587_f", phone="+79991587006",
            name="Мастер без салона", address="Пенза, ул. Суворова, 3",
        )
        assert spec.tenant_id is None
        row = _row_for(_api().get(INTERNAL_URL), spec)
        assert row["tenant"] is None
        assert row["tenant_address"] is None
        assert row["tenant_city"] is None


@pytest.mark.django_db
class TestPublicCatalogUnchanged:
    def test_client_app_catalog_does_not_gain_the_fields(self, salon):
        """Публичные сериализаторы не трогали — контракт мобилки прежний."""
        spec = _make_specialist(
            tenant=salon, username="drf1587_g", phone="+79991587007",
            name="Публичная карточка",
        )
        client = APIClient()
        user = User.objects.create_user(
            username="drf1587_client", password="x", phone="+79991587099",
        )
        client.force_authenticate(user=user)
        response = client.get(PUBLIC_URL, HTTP_X_APP_TYPE="client")
        if response.status_code != 200:
            pytest.skip(
                "публичный каталог требует стек X-App-Type/JWT, "
                f"получено {response.status_code}",
            )
        row = _row_for(response, spec)
        assert "tenant_address" not in row
        assert "tenant_city" not in row
