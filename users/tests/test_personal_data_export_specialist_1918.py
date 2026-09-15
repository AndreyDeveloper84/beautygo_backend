"""DRF-1918 — профиль мастера в выгрузке 152-ФЗ (C5.1).

Выгрузка отдавала клиентский профиль и контекст, но не профиль мастера, а
удаление (D3, ``users.deletion_executor._erase_catalog``) его стирает:
субъект не мог увидеть то, что о нём хранится и будет стёрто.

Сторожа:

* «стирается ⇒ выгружается»: каждое поле из
  ``SPECIALIST_PROFILE_ERASED_FIELDS`` (ровно их сохраняет исполнитель)
  либо в выгрузке, либо в ``SPECIALIST_EXCLUDED_FIELDS`` с причиной;
* каждое поле ``SpecialistProfile`` классифицировано — новое поле без
  решения роняет тест;
* карта полей — то, что ответ действительно несёт;
* портфолио — все строки (M21 ограничивает 10, старый путь пускал 30);
* место (``works_at``): своё (tenant NULL / kind=solo) выгружается, салон —
  «адрес организации», неизвестный вид — «вид места не определён»;
* связанный прокси — со своим профилем мастера (исполнитель стирает и его);
* одна строка журнала PERSONAL_DATA / EXPORT на запрос.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.files.base import ContentFile
from rest_framework.test import APIClient

from privacy_audit.models import PersonalDataAccessLog
from tenants.models import ServiceLocation, Tenant
from users.models import SpecialistPortfolio, SpecialistProfile, User

from .conftest import name_subject

pytestmark = pytest.mark.django_db

EXPORT_URL = "/api/v1/internal/users/{user_id}/personal-data/export/"
TOKEN = "test-bearer-1918"  # pragma: allowlist secret


@pytest.fixture(autouse=True)
def _bearer(settings):
    settings.AYLA_INTERNAL_API_TOKEN = TOKEN


def _export(subject: User) -> dict:
    client = APIClient()
    client.credentials(
        HTTP_AUTHORIZATION=f"Bearer {TOKEN}",
        HTTP_X_EXTERNAL_USER_ID=name_subject(subject),
    )
    resp = client.get(EXPORT_URL.format(user_id=subject.pk))
    assert resp.status_code == 200, resp.content
    return resp.json()["data"]


@pytest.fixture
def master() -> User:
    u = User.objects.create_user(
        username="pd1918-master", password="pass",  # pragma: allowlist secret
        role="specialist",
    )
    sp = u.specialist_profile
    sp.display_name = "Вера Мастерова"
    sp.bio = "стаж, курсы, любимые техники"
    sp.address = "ул. Садовая, 5, кв. 12"
    sp.location_lat = Decimal("53.195000")
    sp.location_lng = Decimal("45.018000")
    sp.experience_years = 7
    sp.timezone = "Europe/Samara"
    sp.provisioned_external_user_id = "bot:max:pd1918-master"
    sp.yookassa_account_id = "yk-sub-pd1918"
    sp.yclients_company_id = "yc-company-pd1918"
    sp.yclients_staff_id = "yc-staff-pd1918"
    sp.save()
    sp.avatar.save("pd1918.jpg", ContentFile(b"a" * 10), save=True)
    return u


def _section(data: dict) -> dict:
    return data["specialist_profile"]


# ---------------------------------------------------------------------------
# Что выгружается
# ---------------------------------------------------------------------------


class TestSpecialistSection:
    def test_exports_the_values_deletion_erases(self, master):
        sp = SpecialistProfile.objects.get(user=master)
        section = _section(_export(master))

        assert section["display_name"] == "Вера Мастерова"
        assert section["bio"] == "стаж, курсы, любимые техники"
        assert section["address"] == "ул. Садовая, 5, кв. 12"
        assert section["location_lat"] == str(sp.location_lat)
        assert section["location_lng"] == str(sp.location_lng)
        assert section["avatar_url"] == sp.avatar.url

    def test_owner_decided_fields_are_exported(self, master):
        section = _section(_export(master))

        assert section["experience_years"] == 7
        assert section["timezone"] == "Europe/Samara"
        assert section["provisioned_external_user_id"] == "bot:max:pd1918-master"
        assert section["yookassa_account_id"] == "yk-sub-pd1918"

    def test_excluded_fields_do_not_leave(self, master):
        data = _export(master)
        section = _section(data)
        text = str(data)

        for key in ("yclients_company_id", "yclients_staff_id", "booking_source", "tenant",
                    "is_available", "is_booking_enabled", "status", "rating", "reviews_count"):
            assert key not in section
        assert "yc-company-pd1918" not in text
        assert "yc-staff-pd1918" not in text

    def test_empty_avatar_and_coordinates_export_null(self, master):
        SpecialistProfile.objects.filter(user=master).update(
            avatar=None, location_lat=None, location_lng=None,
        )
        section = _section(_export(master))

        assert section["avatar_url"] is None
        assert section["location_lat"] is None
        assert section["location_lng"] is None

    def test_client_without_specialist_profile_exports_null_and_creates_nothing(self):
        client_user = User.objects.create_user(
            username="pd1918-client", password="pass",  # pragma: allowlist secret
            role="client",
        )
        data = _export(client_user)

        assert data["specialist_profile"] is None
        assert not SpecialistProfile.objects.filter(user=client_user).exists()


class TestPortfolio:
    def test_all_items_beyond_ten_in_sort_order(self, master):
        sp = SpecialistProfile.objects.get(user=master)
        for i in range(12):
            item = SpecialistPortfolio(specialist=sp, sort_order=12 - i)
            item.image.save(f"pd1918-{i}.jpg", ContentFile(b"p" * 10), save=True)

        portfolio = _section(_export(master))["portfolio"]

        assert len(portfolio) == 12
        assert [p["sort_order"] for p in portfolio] == list(range(1, 13))
        expected = [
            i.image.url for i in SpecialistPortfolio.objects.filter(specialist=sp).order_by(
                "sort_order", "created_at",
            )
        ]
        assert [p["image_url"] for p in portfolio] == expected


class TestWorksAt:
    @staticmethod
    def _place(master: User, tenant: Tenant | None) -> ServiceLocation:
        place = ServiceLocation.objects.create(
            tenant=tenant, address="ул. Рабочая, 3",
            latitude=Decimal("53.100000"), longitude=Decimal("45.100000"),
        )
        SpecialistProfile.objects.filter(user=master).update(works_at=place, tenant=tenant)
        return place

    def test_own_place_without_a_salon_is_exported(self, master):
        place = self._place(master, None)
        assert _section(_export(master))["works_at"] == {
            "kind": "",
            "label": "",
            "address": "ул. Рабочая, 3",
            "note_for_client": "",
            "latitude": str(place.latitude),
            "longitude": str(place.longitude),
        }

    def test_solo_workspace_place_is_exported(self, master):
        solo = Tenant.objects.create(slug="pd1918-solo", name="Соло", kind=Tenant.Kind.SOLO)
        place = self._place(master, solo)
        assert _section(_export(master))["works_at"] == {
            "kind": "",
            "label": "",
            "address": "ул. Рабочая, 3",
            "note_for_client": "",
            "latitude": str(place.latitude),
            "longitude": str(place.longitude),
        }

    def test_salon_place_is_excluded_as_an_organisation_address(self, master):
        salon = Tenant.objects.create(slug="pd1918-salon", name="Салон", kind=Tenant.Kind.SALON)
        self._place(master, salon)
        data = _export(master)

        assert _section(data)["works_at"] == {"excluded": "адрес организации"}
        assert "ул. Рабочая, 3" not in str(data)

    def test_unknown_kind_is_excluded_not_exported_silently(self, master):
        odd = Tenant.objects.create(slug="pd1918-odd", name="Непонятно", kind=Tenant.Kind.SALON)
        Tenant.objects.filter(pk=odd.pk).update(kind="weird")
        self._place(master, Tenant.objects.get(pk=odd.pk))
        data = _export(master)

        assert _section(data)["works_at"] == {"excluded": "вид места не определён"}
        assert "ул. Рабочая, 3" not in str(data)

    def test_no_place_is_null(self, master):
        assert _section(_export(master))["works_at"] is None


class TestLinkedIdentities:
    def test_a_linked_proxys_specialist_profile_is_exported(self, master):
        proxy = User.objects.create(
            username="bot:max:pd1918-proxy", role="client", is_proxy=True,
            is_guest=False, linked_user=master,
        )
        SpecialistProfile.objects.create(user=proxy, display_name="Черновик мастера")

        data = _export(master)
        by_id = {item["external_user_id"]: item for item in data["linked_identities"]}

        assert by_id[proxy.username]["specialist_profile"]["display_name"] == "Черновик мастера"
        assert by_id[name_subject(master)]["specialist_profile"] is None


class TestJournal:
    def test_one_personal_data_export_row_per_request(self, master):
        before = set(PersonalDataAccessLog.objects.values_list("pk", flat=True))
        _export(master)
        rows = PersonalDataAccessLog.objects.exclude(pk__in=before)

        assert rows.count() == 1
        row = rows.get()
        assert row.object_category == PersonalDataAccessLog.ObjectCategory.PERSONAL_DATA
        assert row.operation == PersonalDataAccessLog.Operation.EXPORT
        assert row.object_id == master.pk


# ---------------------------------------------------------------------------
# Сторожа классификации
# ---------------------------------------------------------------------------


class TestErasedImpliesExported:
    def test_every_erased_field_is_exported_or_excluded_with_a_reason(self):
        from users.deletion_executor import SPECIALIST_PROFILE_ERASED_FIELDS
        from users.personal_data_api import SPECIALIST_EXCLUDED_FIELDS, SPECIALIST_EXPORTED_FIELDS

        # Нижняя граница: пустой список прошёл бы «для всех» вхолостую.
        assert len(SPECIALIST_PROFILE_ERASED_FIELDS) >= 6
        assert "display_name" in SPECIALIST_PROFILE_ERASED_FIELDS
        unclassified = []
        for field in SPECIALIST_PROFILE_ERASED_FIELDS:
            exported = field in SPECIALIST_EXPORTED_FIELDS
            reason = SPECIALIST_EXCLUDED_FIELDS.get(field, "").strip()
            if exported == bool(reason):
                unclassified.append(field)
        assert unclassified == []

    def test_every_specialist_profile_field_is_classified(self):
        from users.personal_data_api import SPECIALIST_EXCLUDED_FIELDS, SPECIALIST_EXPORTED_FIELDS

        fields = {f.name for f in SpecialistProfile._meta.concrete_fields}
        assert len(fields) >= 20 and "display_name" in fields
        assert not set(SPECIALIST_EXPORTED_FIELDS) & set(SPECIALIST_EXCLUDED_FIELDS)
        assert fields - set(SPECIALIST_EXPORTED_FIELDS) - set(SPECIALIST_EXCLUDED_FIELDS) == set()
        assert all(reason.strip() for reason in SPECIALIST_EXCLUDED_FIELDS.values())

    def test_the_field_map_is_what_the_response_carries(self, master):
        from users.personal_data_api import SPECIALIST_EXPORTED_FIELDS

        section = _section(_export(master))
        # Кроме полей профиля — связанные строки субъекта: портфолио и зоны выезда
        # (DRF-1803) — списки строк, а не поля ``SpecialistProfile``.
        assert set(section) == set(SPECIALIST_EXPORTED_FIELDS.values()) | {"portfolio", "service_areas"}

    def test_the_executor_saves_exactly_the_declared_erased_fields(self, master):
        from users.deletion_executor import ERASED_NAME, SPECIALIST_PROFILE_ERASED_FIELDS, _erase_catalog

        saved: list = []
        original = SpecialistProfile.save

        def spy(self, *args, **kwargs):
            saved.append(kwargs.get("update_fields"))
            return original(self, *args, **kwargs)

        with patch.object(SpecialistProfile, "save", spy):
            _erase_catalog(master)

        assert [*SPECIALIST_PROFILE_ERASED_FIELDS, "updated_at"] in saved
        sp = SpecialistProfile.objects.get(user=master)
        assert (sp.display_name, sp.bio, sp.address) == (ERASED_NAME, "", "")
        assert (sp.location_lat, sp.location_lng, sp.is_available, sp.is_booking_enabled) == (
            None, None, False, False,
        )
        assert not sp.avatar
