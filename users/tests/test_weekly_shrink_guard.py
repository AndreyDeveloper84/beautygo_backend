"""Сокращение рамки не оставляет клиента без мастера — DRF-1297 B-4.

Пробел, закрытый 10.09.2026. Два отрицательных эталона, стоявших на его
открытости, перевёрнуты в
``tenants/tests/test_schedule_conflict_guard_1297.py``; здесь то, чего они
не проверяли.

# Что здесь держится, кроме отказа

**Запись, уже стоявшая ВНЕ рамки, менять расписание не мешает.** Это не
поблажка, а условие правильности. Наивная проверка «есть запись вне новых
часов → конфликт» неверна: walk-in и салонная запись создаются вне рамки
намеренно (``check_schedule_frame`` зовётся только для actor_role ==
"user"). Салон, однажды записавший клиента в нерабочее время, иначе никогда
больше не смог бы поправить график. Виновата только та запись, которая была
ВНУТРИ старой рамки и оказалась ВНЕ новой.

Без этого теста сторож нельзя отличить от наивного: на отказе они ведут
себя одинаково, и расходятся ровно здесь.

**Обе двери.** Дверей две — своя у мастера (``/specialists/me/schedule/``)
и салонная (``/tenants/me/masters/{id}/schedule/``, наследник, зовущий тот
же ``super().put()``). Сторож стоит в базовом классе; доказательство через
одного наследника не доказывает, что закрыта вторая.

**Расширение рамки проходит.** Сторож обязан молчать, когда никого не
вытесняют, иначе он превращается в запрет менять график вообще.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment, SpecialistWorkingHours
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

MSK = ZoneInfo("Europe/Moscow")
#: Вторник далеко впереди — намеренно ДАЛЬШЕ горизонта записи (60 дней).
#: Первая версия сторожа ограничивалась горизонтом и на такой записи
#: молчала; тест стоит здесь, чтобы это не вернулось.
TARGET = dt.date(2026, 12, 8)


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="wsg", name="Weekly Shrink Salon")


@pytest.fixture
def master_user(db):
    return User.objects.create_user(
        username="wsg_master", password="x", role="specialist", phone="+79995401001",
    )


@pytest.fixture
def master(master_user, salon):
    profile = master_user.specialist_profile
    profile.display_name = "Ольга"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.tenant = salon
    profile.timezone = "Europe/Moscow"
    profile.save(update_fields=["display_name", "status", "tenant", "timezone"])
    return profile


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="wsg_client", password="x", role="client", phone="+79995401002",
    )


@pytest.fixture
def admin_user(salon):
    user = User.objects.create_user(
        username="wsg_admin", password="x", role="client", phone="+79995401003",
    )
    TenantUserRelationship.objects.create(
        user=user, tenant=salon,
        role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return user


@pytest.fixture
def service(master, db):
    category = ServiceCategory.objects.create(name="WSG", slug="wsg-cat")
    return Service.objects.create(
        specialist=master, category=category, name="Стрижка",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def pro_api(master_user, master):
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    client.force_authenticate(user=master_user)
    return client


@pytest.fixture
def salon_api(admin_user, salon):
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    client.defaults["HTTP_X_TENANT"] = salon.slug
    client.force_authenticate(user=admin_user)
    return client


def _working_tuesday(master, *, start=dt.time(10, 0), end=dt.time(19, 0)):
    return SpecialistWorkingHours.objects.create(
        specialist=master,
        day_of_week=TARGET.weekday(),
        is_working_day=True,
        start_time=start,
        end_time=end,
    )


def _booking(salon, customer, master, service, *, at_local=dt.time(14, 0), minutes=60):
    start = dt.datetime.combine(TARGET, at_local, tzinfo=MSK)
    return Appointment.objects.create(
        tenant=salon,
        client=customer,
        specialist=master,
        service=service,
        salon_service=None,
        start_datetime=start,
        end_datetime=start + dt.timedelta(minutes=minutes),
        status=Appointment.Status.CONFIRMED,
        price=Decimal("2000.00"),
    )


def _close_tuesday(api, url):
    return api.patch(
        url,
        {"schedule": [{
            "day_of_week": TARGET.weekday(), "is_working_day": False,
            "start_time": None, "end_time": None,
        }]},
        format="json",
    )


PRO_URL = "/api/v1/specialists/me/schedule/"


def _salon_url(master) -> str:
    return f"/api/v1/tenants/me/masters/{master.id}/schedule/"


@pytest.mark.django_db
class TestABookingAlreadyOutsideTheFrameDoesNotBlock:
    """Условие правильности, а не поблажка — см. шапку файла."""

    def test_a_booking_placed_outside_working_hours_still_lets_the_schedule_change(
        self, salon, master, customer, service, pro_api
    ):
        # 21:00 при рамке 10:00–19:00: так выглядит запись, которую салон
        # поставил вне графика намеренно. Она была вне СТАРОЙ рамки, значит
        # новой не вытеснена.
        _working_tuesday(master)
        _booking(salon, customer, master, service, at_local=dt.time(21, 0))

        resp = _close_tuesday(pro_api, PRO_URL)

        assert resp.status_code == 200, resp.data

    def test_the_same_change_is_refused_when_the_booking_did_fit(
        self, salon, master, customer, service, pro_api
    ):
        # Положительный контроль к тесту выше, и он обязан стоять рядом:
        # без него «проходит» ничего не значит — сторож мог не работать
        # вовсе. Единственная разница между тестами — время записи.
        _working_tuesday(master)
        _booking(salon, customer, master, service, at_local=dt.time(14, 0))

        resp = _close_tuesday(pro_api, PRO_URL)

        assert resp.status_code == 409, resp.data
        assert resp.data["error"]["code"] == "HAS_ACTIVE_APPOINTMENTS"


@pytest.mark.django_db
class TestBothDoorsAreClosed:
    """Сторож стоит в базовом классе; проверяем это с обеих сторон."""

    def test_the_masters_own_door_refuses(self, salon, master, customer, service, pro_api):
        _working_tuesday(master)
        _booking(salon, customer, master, service)

        assert _close_tuesday(pro_api, PRO_URL).status_code == 409

    def test_the_salon_door_refuses(self, salon, master, customer, service, salon_api):
        _working_tuesday(master)
        _booking(salon, customer, master, service)

        assert _close_tuesday(salon_api, _salon_url(master)).status_code == 409


@pytest.mark.django_db
class TestTheGuardStaysQuietWhenNobodyIsDisplaced:
    def test_widening_the_day_passes(self, salon, master, customer, service, pro_api):
        # Сторож, срабатывающий на расширении, — это запрет менять график.
        _working_tuesday(master, start=dt.time(12, 0), end=dt.time(16, 0))
        _booking(salon, customer, master, service)

        resp = pro_api.patch(
            PRO_URL,
            {"schedule": [{
                "day_of_week": TARGET.weekday(), "is_working_day": True,
                "start_time": "09:00", "end_time": "21:00",
            }]},
            format="json",
        )

        assert resp.status_code == 200, resp.data

    def test_a_cancelled_booking_does_not_block(self, salon, master, customer, service, pro_api):
        _working_tuesday(master)
        booking = _booking(salon, customer, master, service)
        booking.status = Appointment.Status.CANCELLED
        booking.save(update_fields=["status"])

        assert _close_tuesday(pro_api, PRO_URL).status_code == 200

    def test_another_masters_booking_does_not_block(
        self, salon, master, customer, service, pro_api, db
    ):
        other_user = User.objects.create_user(
            username="wsg_other", password="x", role="specialist", phone="+79995401004",
        )
        other = other_user.specialist_profile
        other.tenant = salon
        other.timezone = "Europe/Moscow"
        other.save(update_fields=["tenant", "timezone"])

        _working_tuesday(master)
        _booking(salon, customer, other, service)

        assert _close_tuesday(pro_api, PRO_URL).status_code == 200

    def test_nothing_is_written_when_the_change_is_refused(
        self, salon, master, customer, service, pro_api
    ):
        # Отказ обязан быть полным: 409 с наполовину применённой правкой
        # хуже молчаливого 200 — он оставляет расписание в состоянии,
        # которого не просил никто.
        _working_tuesday(master)
        _booking(salon, customer, master, service)

        assert _close_tuesday(pro_api, PRO_URL).status_code == 409

        row = SpecialistWorkingHours.objects.get(
            specialist=master, day_of_week=TARGET.weekday(),
        )
        assert row.is_working_day is True
        assert row.start_time == dt.time(10, 0)
