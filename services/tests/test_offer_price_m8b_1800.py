"""DRF-1800 (M8b): цена и длительность выбранной услуги, «Убрать из моих услуг».

``PUT /api/v1/internal/specialists/{id}/services/{salon_service_id}/offer/`` и
``DELETE /api/v1/internal/specialists/{id}/services/{salon_service_id}/`` под
``IsInternalBearerForSpecialistSubject``. Что стережётся:

* первая цена создаёт ``SpecialistService`` мастера, следующая — обновляет ту
  же строку; ``configured`` в ответе растёт только от сервера;
* цена < 1, длительность вне 5..480, пропуск поля — 400, ничего не создано;
* услуга не из своего workspace (чужая или несуществующая) — 404, чужой
  workspace по URL — 403, убранная услуга — 409 ``service_removed``;
* удаление при будущей живой записи — 409 ``HAS_APPOINTMENTS`` с числом,
  ничего не тронуто; без записей и без решения модератора — строка удаляется;
  с прошлой записью или решённой связью — выключается (история и разметка
  остаются); повторный выбор включает её обратно.

Каждый отказ рядом с положительной половиной на тех же данных.
Красный до правки: весь файл (маршрутов нет → 404).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from appointments.models import Appointment
from services.models import SalonService, SpecialistService
from services.tests import test_offer_selection_m8a_1800 as m8a
from users.models import User

pytestmark = pytest.mark.django_db

# Те же данные, что у M8a: соло-мастера, канон, токен. Фикстуры — через
# присваивание, а не импорт имён (иначе аргумент теста «переопределяет импорт»).
_tokens = m8a._tokens
olga = m8a.olga
irina = m8a.irina
nails = m8a.nails
manicure = m8a.manicure
pedicure = m8a.pedicure
OLGA = m8a.OLGA
_client = m8a._client
_rows = m8a._rows
_select = m8a._select
_selection_url = m8a._url


def _offer_url(profile, row) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/services/{row.pk}/offer/"


def _service_url(profile, row) -> str:
    return f"/api/v1/internal/specialists/{profile.pk}/services/{row.pk}/"


def _put(profile, row, *, price="1500", duration=45, actor=OLGA, body=None):
    payload = {"price": price, "duration_minutes": duration} if body is None else body
    return _client(actor).put(_offer_url(profile, row), payload, format="json")


def _selected_row(profile, template) -> SalonService:
    assert _select(profile, template).status_code in (200, 201)
    return _rows(profile).get(template=template)


def _booking(profile, row, *, start, status):
    client = User.objects.create_user(
        username=f"client_m8b_{uuid.uuid4().hex[:8]}",
        password="x",  # pragma: allowlist secret
        role="client",
        phone=f"+7999518{uuid.uuid4().int % 10000:04d}",
    )
    return Appointment.objects.create(
        client=client,
        specialist=profile,
        tenant=profile.tenant,
        salon_service=row,
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=status,
        price=Decimal("1500"),
    )


class TestTheFirstPriceCreatesTheOffer:
    def test_put_creates_an_active_offer_and_the_service_becomes_configured(
        self, olga, manicure, pedicure,
    ):
        assert _select(olga, manicure, pedicure).status_code == 201
        row = _rows(olga).get(template=manicure)

        r = _put(olga, row, price="1500", duration=45)

        assert r.status_code == 201, r.content
        offer = SpecialistService.objects.get(specialist=olga, salon_service=row)
        assert offer.price == Decimal("1500.00")
        assert offer.duration_minutes == 45
        assert offer.is_active is True
        data = r.json()["data"]
        assert data["offer_id"] == str(offer.pk)
        assert data["selected"] == 2 and data["configured"] == 1
        items = {item["salon_service_id"]: item for item in data["services"]}
        assert items[str(row.pk)]["offer"]["duration_minutes"] == 45

    def test_a_second_put_updates_the_same_offer(self, olga, manicure):
        row = _selected_row(olga, manicure)
        first = _put(olga, row, price="1500", duration=45)
        assert first.status_code == 201, first.content

        second = _put(olga, row, price="1800", duration=60)

        assert second.status_code == 200, second.content
        offers = SpecialistService.objects.filter(specialist=olga, salon_service=row)
        assert offers.count() == 1
        assert offers.get().pk == uuid.UUID(first.json()["data"]["offer_id"])
        assert offers.get().price == Decimal("1800.00") and offers.get().duration_minutes == 60


class TestOfferRefusals:
    @pytest.mark.parametrize(
        "body",
        [
            {"price": "0", "duration_minutes": 45},
            {"price": "-5", "duration_minutes": 45},
            {"price": "1500", "duration_minutes": 4},
            {"price": "1500", "duration_minutes": 481},
            {"price": "1500"},
        ],
        ids=["price-0", "price-negative", "duration-4", "duration-481", "duration-missing"],
    )
    def test_an_invalid_price_or_duration_is_400_and_nothing_is_created(self, olga, manicure, body):
        row = _selected_row(olga, manicure)

        r = _put(olga, row, body=body)

        assert r.status_code == 400, r.content
        assert SpecialistService.objects.filter(specialist=olga).count() == 0
        # Положительная половина: та же услуга с верными числами.
        assert _put(olga, row).status_code == 201

    def test_a_service_outside_the_workspace_is_404(self, olga, irina, manicure):
        own = _selected_row(olga, manicure)
        foreign = SalonService.objects.create(
            tenant=irina.tenant, template=manicure, category=manicure.category, name=manicure.name,
        )
        ghost = SalonService(pk=uuid.uuid4())

        for row in (foreign, ghost):
            r = _put(olga, row)
            assert r.status_code == 404, r.content
            assert r.json()["error"]["details"] == {"reason": "service_not_selected"}
        assert SpecialistService.objects.count() == 0
        assert _put(olga, own).status_code == 201

    def test_another_masters_workspace_is_403(self, olga, irina, manicure):
        own = _selected_row(olga, manicure)
        foreign = SalonService.objects.create(
            tenant=irina.tenant, template=manicure, category=manicure.category, name=manicure.name,
        )

        r = _put(irina, foreign, actor=OLGA)

        assert r.status_code == 403, r.content
        assert SpecialistService.objects.filter(salon_service=foreign).count() == 0
        assert _put(olga, own, actor=OLGA).status_code == 201

    def test_a_removed_service_needs_to_be_selected_again(self, olga, manicure):
        row = _selected_row(olga, manicure)
        _booking(olga, row, start=timezone.now() - timedelta(days=3), status=Appointment.Status.COMPLETED)
        assert _client().delete(_service_url(olga, row)).json()["data"]["removal"] == "deactivated"

        refused = _put(olga, row)

        assert refused.status_code == 409, refused.content
        assert refused.json()["error"]["details"] == {"reason": "service_removed"}
        # Положительная половина: выбрал снова — та же строка включена, цена ставится.
        again = _select(olga, manicure)
        assert again.status_code == 201, again.content
        assert _rows(olga).get(template=manicure).pk == row.pk
        assert _put(olga, row).status_code == 201


class TestRemoval:
    def test_without_bookings_the_row_and_the_offer_are_deleted(self, olga, manicure):
        row = _selected_row(olga, manicure)
        assert _put(olga, row).status_code == 201

        r = _client().delete(_service_url(olga, row))

        assert r.status_code == 200, r.content
        data = r.json()["data"]
        assert data["removal"] == "deleted"
        assert data["selected"] == 0 and data["configured"] == 0
        assert not SalonService.objects.filter(pk=row.pk).exists()
        assert SpecialistService.objects.filter(specialist=olga).count() == 0

    def test_a_future_booking_blocks_removal_and_touches_nothing(self, olga, manicure):
        row = _selected_row(olga, manicure)
        assert _put(olga, row).status_code == 201
        booking = _booking(
            olga, row, start=timezone.now() + timedelta(days=2), status=Appointment.Status.CONFIRMED,
        )

        r = _client().delete(_service_url(olga, row))

        assert r.status_code == 409, r.content
        assert r.json()["error"]["code"] == "HAS_APPOINTMENTS"
        assert r.json()["error"]["details"] == {"reason": "has_future_appointments", "count": 1}
        row.refresh_from_db()
        assert row.is_active is True
        assert SpecialistService.objects.get(specialist=olga, salon_service=row).is_active is True
        # Положительная половина: запись отменена — убрать можно; строку держит история.
        booking.status = Appointment.Status.CANCELLED
        booking.save(update_fields=["status"])
        after = _client().delete(_service_url(olga, row))
        assert after.status_code == 200, after.content
        assert after.json()["data"]["removal"] == "deactivated"

    def test_a_past_booking_keeps_the_row_and_reselect_brings_it_back(self, olga, manicure):
        row = _selected_row(olga, manicure)
        assert _put(olga, row).status_code == 201
        _booking(olga, row, start=timezone.now() - timedelta(days=3), status=Appointment.Status.COMPLETED)

        r = _client().delete(_service_url(olga, row))

        assert r.status_code == 200, r.content
        assert r.json()["data"]["removal"] == "deactivated"
        assert r.json()["data"]["selected"] == 0 and r.json()["data"]["configured"] == 0
        row.refresh_from_db()
        assert row.is_active is False
        assert SpecialistService.objects.get(specialist=olga, salon_service=row).is_active is False

        assert _select(olga, manicure).status_code == 201
        assert _put(olga, row).status_code == 200  # то же предложение, включено
        state = _client().get(_selection_url(olga.pk)).json()["data"]
        assert state["selected"] == 1 and state["configured"] == 1

    def test_a_decided_mapping_is_deactivated_not_deleted(self, olga, manicure):
        decided = SalonService.objects.create(
            tenant=olga.tenant,
            template=manicure,
            category=manicure.category,
            name=manicure.name,
            mapping_status=SalonService.MappingStatus.VERIFIED,
            mapping_confirmed_at=timezone.now(),
            mapping_confirmed_rule="owner_review",
            mapping_rule_version="1",
            mapping_source_ref="review-1800b",
        )

        r = _client().delete(_service_url(olga, decided))

        assert r.status_code == 200, r.content
        assert r.json()["data"]["removal"] == "deactivated"
        decided.refresh_from_db()
        assert decided.is_active is False
        assert decided.mapping_status == SalonService.MappingStatus.VERIFIED

    def test_removing_outside_the_workspace_is_404_or_403(self, olga, irina, manicure):
        own = _selected_row(olga, manicure)
        foreign = SalonService.objects.create(
            tenant=irina.tenant, template=manicure, category=manicure.category, name=manicure.name,
        )

        assert _client().delete(_service_url(olga, foreign)).status_code == 404
        assert _client().delete(_service_url(irina, foreign)).status_code == 403
        assert SalonService.objects.filter(pk=foreign.pk).exists()
        assert _client().delete(_service_url(olga, own)).status_code == 200
