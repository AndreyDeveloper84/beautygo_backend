"""DRF-1803 (M11) — место работы соло-мастера: один путь записи под субъектом.

Что стережётся:

* место, названное мастером, — ``REVIEW_REQUIRED`` в его workspace, город — из
  workspace; подтверждения мастер поставить не может (``status`` во вводе — 400);
* «два формата = две строки»: место и зона выезда — разные строки;
* «LATER не даёт WHOLE_CITY молча»: охват обязателен и хранится как назван;
* чужой ``tenant_id`` во вводе — 400, место остаётся в своём workspace;
* название студии — только отображение: связей с тенантом не создаёт;
* второе своё место — отказ по имени, не молчаливая замена (несколько мест — DRF-1956);
* сменился адрес — подтверждение и координаты прежнего адреса сняты; сменилась
  подсказка клиенту — подтверждение остаётся;
* субъект: чужой workspace — 403, чужое место — 404, мастер салона — 409 по имени;
* журнал §96: чтение и запись места — по строке;
* 152-ФЗ: подсказка клиенту и зона выезда выгружаются и стираются при удалении аккаунта.

Каждый отказ — рядом с положительной половиной на тех же данных.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from privacy_audit.models import PersonalDataAccessLog
from tenants.models import GeocodeStatus, LocationStatus, ServiceArea, ServiceLocation, Tenant
from tenants.solo_provisioning import provision_solo_workspace
from users.deletion_executor import BotConfirmation, execute
from users.deletion_requests import ensure_deletion_request
from users.models import SpecialistProfile, TenantUserRelationship, User

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1803"  # noqa: S105
OLGA = "bot:max:1803001"
IRINA = "bot:max:1803002"
LIST = "/api/v1/internal/specialists/{sid}/service-locations/"
ITEM = "/api/v1/internal/specialists/{sid}/service-locations/{item}/"
EXPORT = "/api/v1/internal/users/{user_id}/personal-data/export/"

CABINET = {"kind": "private_studio", "address": "ул. Пушкина, 45", "note_for_client": "Вход со двора, 2 этаж"}


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN


def _provision(external_user_id: str, slug: str) -> SpecialistProfile:
    return provision_solo_workspace(
        tenant_id=uuid.uuid4(),
        slug=slug,
        name=f"Студия {slug}",
        city="Пенза",
        external_user_id=external_user_id,
        display_name="Мастер",
    ).profile


@pytest.fixture
def olga() -> SpecialistProfile:
    return _provision(OLGA, "solo-max-1803olga")


@pytest.fixture
def irina() -> SpecialistProfile:
    return _provision(IRINA, "solo-max-1803irin")


@pytest.fixture
def operator() -> User:
    return User.objects.create_superuser(
        username="op-1803", password="pw", email="op-1803@x.y", role="admin",  # pragma: allowlist secret
    )


@pytest.fixture
def salon_master() -> SpecialistProfile:
    salon = Tenant.objects.create(slug="salon-1803", name="Салон 1803", city="Пенза", kind=Tenant.Kind.SALON)
    user = User.objects.create_user(
        username="salon-master-1803", password="x", role="specialist", phone="+79990001803",  # pragma: allowlist secret
    )
    SpecialistProfile.objects.filter(user=user).update(tenant=salon, display_name="Мастер салона")
    return SpecialistProfile.objects.get(user=user)


def _linked_header(user: User) -> str:
    """Внешняя личность, связанная с ``user`` (proxy → linked_user), — как в бою после связи."""
    external_id = f"bot:test:{user.pk.hex[:12]}"
    User.objects.get_or_create(
        username=external_id,
        defaults={"role": "client", "is_proxy": True, "is_guest": False, "linked_user": user},
    )
    return external_id


def _client(actor: str) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _post(profile: SpecialistProfile, body: dict, actor: str = OLGA):
    return _client(actor).post(LIST.format(sid=profile.pk), body, format="json")


def _patch(profile: SpecialistProfile, item, body: dict, actor: str = OLGA):
    return _client(actor).patch(ITEM.format(sid=profile.pk, item=item), body, format="json")


def _reason(resp) -> str:
    return resp.json()["error"]["details"]["reason"]


def _confirm(place_id, operator: User) -> None:
    ServiceLocation.objects.filter(pk=place_id).update(
        status=LocationStatus.CONFIRMED, confirmed_by=operator, confirmed_at=timezone.now(),
        confirmed_source_ref="модерация профиля", latitude=Decimal("53.200000"),
        longitude=Decimal("45.000000"), geocode_status=GeocodeStatus.OK, geocode_provider="test",
    )


class TestCreate:
    def test_cabinet_is_saved_as_review_required_in_own_workspace(self, olga):
        resp = _post(olga, CABINET)

        assert resp.status_code == 201, resp.content
        data = resp.json()["data"]
        assert data["city"] == "Пенза"
        [place] = data["places"]
        assert place["kind"] == "private_studio"
        assert place["address"] == "ул. Пушкина, 45"
        assert place["note_for_client"] == "Вход со двора, 2 этаж"
        assert place["city"] == "Пенза"
        assert place["status"] == "review_required"
        assert place["shown_to_clients_after_publication"] is False
        row = ServiceLocation.objects.get(pk=place["id"])
        assert row.tenant_id == olga.tenant_id
        assert row.confirmed_by is None and row.confirmed_source_ref == ""
        assert row.geocode_status == GeocodeStatus.NOT_ATTEMPTED and row.latitude is None
        olga.refresh_from_db()
        assert olga.works_at_id == row.pk

    def test_studio_name_is_display_only_and_creates_no_tenant_links(self, olga):
        links = TenantUserRelationship.objects.count()
        tenants = Tenant.all_objects.count()

        resp = _post(olga, {"kind": "salon_or_studio", "address": "ул. Московская, 29", "label": "Beauty Loft"})

        assert resp.status_code == 201, resp.content
        assert resp.json()["data"]["places"][0]["label"] == "Beauty Loft"
        assert TenantUserRelationship.objects.count() == links
        assert Tenant.all_objects.count() == tenants

    def test_cabinet_and_travel_are_two_rows(self, olga):
        assert _post(olga, CABINET).status_code == 201
        resp = _post(olga, {"kind": "mobile", "coverage": "whole_city"})

        assert resp.status_code == 201, resp.content
        data = resp.json()["data"]
        assert len(data["places"]) == 1
        assert data["areas"] == [{
            "id": data["areas"][0]["id"], "kind": "mobile", "city": "Пенза",
            "coverage": "whole_city", "configured": True,
        }]

    def test_later_is_stored_as_later_not_as_whole_city(self, olga):
        missing = _post(olga, {"kind": "mobile"})
        assert missing.status_code == 400
        assert ServiceArea.objects.filter(specialist=olga).count() == 0

        resp = _post(olga, {"kind": "mobile", "coverage": "later"})

        assert resp.status_code == 201, resp.content
        [area] = resp.json()["data"]["areas"]
        assert area["coverage"] == "later" and area["configured"] is False
        assert ServiceArea.objects.get(specialist=olga).coverage == "later"


class TestValidation:
    def test_foreign_tenant_id_in_the_body_is_refused(self, olga, irina):
        refused = _post(olga, {**CABINET, "tenant_id": str(irina.tenant_id)})
        assert refused.status_code == 400
        assert refused.json()["error"]["details"]["unknown_fields"] == ["tenant_id"]
        assert ServiceLocation.objects.count() == 0

        resp = _post(olga, CABINET)
        assert resp.status_code == 201
        assert ServiceLocation.objects.get().tenant_id == olga.tenant_id

    def test_note_of_201_characters_is_refused_200_is_saved(self, olga):
        assert _post(olga, {**CABINET, "note_for_client": "я" * 201}).status_code == 400
        assert ServiceLocation.objects.count() == 0

        resp = _post(olga, {**CABINET, "note_for_client": "я" * 200})
        assert resp.status_code == 201, resp.content
        assert len(resp.json()["data"]["places"][0]["note_for_client"]) == 200

    def test_blank_address_and_unknown_kind_are_refused(self, olga):
        assert _post(olga, {**CABINET, "address": "   "}).status_code == 400
        assert _post(olga, {**CABINET, "kind": "palace"}).status_code == 400
        assert ServiceLocation.objects.count() == 0
        assert _post(olga, CABINET).status_code == 201

    def test_second_own_place_is_a_named_refusal_not_a_replacement(self, olga):
        first = _post(olga, CABINET)
        assert first.status_code == 201
        first_id = first.json()["data"]["places"][0]["id"]

        second = _post(olga, {"kind": "salon_or_studio", "address": "ул. Московская, 29"})

        assert second.status_code == 409
        assert _reason(second) == "place_already_set"
        olga.refresh_from_db()
        assert str(olga.works_at_id) == first_id
        assert ServiceLocation.objects.count() == 1

    def test_second_travel_zone_is_a_named_refusal(self, olga):
        assert _post(olga, {"kind": "mobile", "coverage": "later"}).status_code == 201
        second = _post(olga, {"kind": "mobile", "coverage": "whole_city"})
        assert second.status_code == 409
        assert _reason(second) == "area_already_set"
        assert ServiceArea.objects.get(specialist=olga).coverage == "later"

    def test_master_cannot_confirm_their_own_place(self, olga):
        place_id = _post(olga, CABINET).json()["data"]["places"][0]["id"]

        refused = _patch(olga, place_id, {"status": "confirmed"})
        assert refused.status_code == 400
        assert refused.json()["error"]["details"]["unknown_fields"] == ["status"]
        assert ServiceLocation.objects.get(pk=place_id).status == LocationStatus.REVIEW_REQUIRED

        assert _patch(olga, place_id, {"note_for_client": "Домофон 12"}).status_code == 200


class TestUpdate:
    def test_address_change_drops_confirmation_and_coordinates(self, olga, operator):
        place_id = _post(olga, CABINET).json()["data"]["places"][0]["id"]
        _confirm(place_id, operator)

        resp = _patch(olga, place_id, {"address": "ул. Московская, 29"})

        assert resp.status_code == 200, resp.content
        row = ServiceLocation.objects.get(pk=place_id)
        assert row.address == "ул. Московская, 29"
        assert row.status == LocationStatus.REVIEW_REQUIRED
        assert row.confirmed_by is None and row.confirmed_at is None and row.confirmed_source_ref == ""
        assert row.latitude is None and row.longitude is None
        assert row.geocode_status == GeocodeStatus.NOT_ATTEMPTED and row.geocode_provider == ""

    def test_note_change_keeps_confirmation_and_coordinates(self, olga, operator):
        place_id = _post(olga, CABINET).json()["data"]["places"][0]["id"]
        _confirm(place_id, operator)

        resp = _patch(olga, place_id, {"note_for_client": "Кабинет 204"})

        assert resp.status_code == 200, resp.content
        row = ServiceLocation.objects.get(pk=place_id)
        assert row.note_for_client == "Кабинет 204"
        assert row.status == LocationStatus.CONFIRMED and row.confirmed_by == operator
        assert row.latitude == Decimal("53.200000")
        assert resp.json()["data"]["places"][0]["shown_to_clients_after_publication"] is True

    def test_spacing_only_address_edit_keeps_confirmation(self, olga, operator):
        place_id = _post(olga, CABINET).json()["data"]["places"][0]["id"]
        _confirm(place_id, operator)

        assert _patch(olga, place_id, {"address": "ул.  Пушкина,  45"}).status_code == 200

        assert ServiceLocation.objects.get(pk=place_id).status == LocationStatus.CONFIRMED

    def test_travel_zone_coverage_is_changed_by_patch(self, olga):
        area_id = _post(olga, {"kind": "mobile", "coverage": "later"}).json()["data"]["areas"][0]["id"]

        refused = _patch(olga, area_id, {"coverage": "some_districts"})
        assert refused.status_code == 400
        resp = _patch(olga, area_id, {"coverage": "whole_city"})

        assert resp.status_code == 200, resp.content
        assert ServiceArea.objects.get(pk=area_id).coverage == "whole_city"


class TestSubject:
    def test_foreign_workspace_is_403_own_is_200(self, olga, irina):
        foreign = _client(IRINA).get(LIST.format(sid=olga.pk))
        assert foreign.status_code == 403
        assert _post(olga, CABINET, actor=IRINA).status_code == 403
        assert ServiceLocation.objects.count() == 0

        own = _client(OLGA).get(LIST.format(sid=olga.pk))
        assert own.status_code == 200, own.content
        assert own.json()["data"] == {
            "specialist_id": str(olga.pk), "city": "Пенза", "places": [], "areas": [],
        }

    def test_foreign_place_or_zone_under_own_url_is_404(self, olga, irina):
        irina_place = _post(irina, CABINET, actor=IRINA).json()["data"]["places"][0]["id"]
        irina_area = _post(irina, {"kind": "mobile", "coverage": "later"}, actor=IRINA).json()["data"]["areas"][0]["id"]

        assert _patch(olga, irina_place, {"note_for_client": "чужое"}).status_code == 404
        assert _patch(olga, irina_area, {"coverage": "whole_city"}).status_code == 404
        assert ServiceLocation.objects.get(pk=irina_place).note_for_client == CABINET["note_for_client"]
        assert ServiceArea.objects.get(pk=irina_area).coverage == "later"

        own_place = _post(olga, CABINET).json()["data"]["places"][0]["id"]
        assert _patch(olga, own_place, {"note_for_client": "своё"}).status_code == 200

    def test_salon_master_is_refused_by_name(self, salon_master, olga):
        actor = _linked_header(salon_master.user)

        read = _client(actor).get(LIST.format(sid=salon_master.pk))
        assert read.status_code == 409
        assert _reason(read) == "salon_place_owner_managed"
        write = _post(salon_master, CABINET, actor=actor)
        assert write.status_code == 409
        assert _reason(write) == "salon_place_owner_managed"
        assert ServiceLocation.objects.count() == 0

        assert _post(olga, CABINET).status_code == 201


class TestJournal:
    def test_read_and_write_each_leave_one_row(self, olga):
        op = PersonalDataAccessLog.Operation

        assert _client(OLGA).get(LIST.format(sid=olga.pk)).status_code == 200
        assert _post(olga, CABINET).status_code == 201

        reads = PersonalDataAccessLog.objects.filter(operation=op.READ_SERVICE_LOCATION)
        writes = PersonalDataAccessLog.objects.filter(operation=op.WRITE_SERVICE_LOCATION)
        assert reads.count() == 1 and writes.count() == 1
        assert {row.object_category for row in [*reads, *writes]} == {
            PersonalDataAccessLog.ObjectCategory.SPECIALIST_PROFILE,
        }


class TestPersonalData:
    def test_note_and_travel_zone_are_exported(self, olga):
        assert _post(olga, CABINET).status_code == 201
        assert _post(olga, {"kind": "mobile", "coverage": "whole_city"}).status_code == 201

        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RUNTIME_TOKEN}",
            HTTP_X_EXTERNAL_USER_ID=_linked_header(olga.user),
        )
        resp = client.get(EXPORT.format(user_id=olga.user_id))

        assert resp.status_code == 200, resp.content
        section = resp.json()["data"]["specialist_profile"]
        assert section["works_at"]["note_for_client"] == "Вход со двора, 2 этаж"
        assert section["works_at"]["kind"] == "private_studio"
        assert section["service_areas"] == [{"kind": "mobile", "city": "Пенза", "coverage": "whole_city"}]

    def test_note_is_erased_and_travel_zone_deleted_with_the_account(self, olga):
        place_id = _post(olga, CABINET).json()["data"]["places"][0]["id"]
        assert _post(olga, {"kind": "mobile", "coverage": "whole_city"}).status_code == 201
        assert ServiceArea.objects.filter(specialist=olga).count() == 1

        class _BotOk:
            def confirm(self, **kw):
                return BotConfirmation(True, {"all_ok": True, "steps": ["memory_delete"], "flag_cleared": True})

        req = ensure_deletion_request(olga.user, initiator="bot").request
        out = execute(req, bot_client=_BotOk())
        assert out.completed, out
        req.refresh_from_db()

        place = ServiceLocation.objects.get(pk=place_id)
        assert (place.address, place.note_for_client) == ("", "")
        assert place.status == LocationStatus.INACTIVE
        assert ServiceArea.objects.filter(specialist=olga).count() == 0
        assert req.steps["anonymised"]["tenants.ServiceArea.deleted"] == 1
