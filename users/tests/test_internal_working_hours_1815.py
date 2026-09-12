"""Часы мастера из кабинета — под субъектом (DRF-1815, M23; макет 7.1–7.5).

До этого у каталога была одна дверь записи часов — Pro-JWT
``/specialists/me/schedule/``; у бота ни одного метода записи. Теперь
``GET/PUT /internal/specialists/{id}/working-hours/`` под
``IsInternalBearerForSpecialistSubject`` (ruling D1).

Что заперто:

- свой профиль: PUT сохраняет 7 дней, GET читает то же; ответ несёт
  ``timezone`` профиля; часы читает ``AvailabilityQueryService``-путь
  (та же таблица ``SpecialistWorkingHours``);
- та же валидация: ``start ≥ end`` → 400; 6 дней → 400;
- та же усадочная защита: сокращение поверх будущей записи → 409
  ``HAS_ACTIVE_APPOINTMENTS``, часы не тронуты;
- чужой профиль → 403 ``subject_mismatch``, часы не тронуты;
- прокси без связи → 403 ``subject_unresolved`` (до M28 писать некуда —
  при подмене сторожа краснеет ровно этот тест);
- без ``X-External-User-ID`` → 403; provisioning-токен → 403;
- несуществующий профиль (свой UUID не совпадёт) — 403, не 404: UUID не
  подтверждается.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment, SpecialistWorkingHours
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1815"  # noqa: S105
PROVISIONING_TOKEN = "test-provisioning-only-token-1815"  # noqa: S105
MSK = ZoneInfo("Europe/Moscow")


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN
    settings.AYLA_IDENTITY_PROVISIONING_TOKEN = PROVISIONING_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="wh1815", name="WH Salon")


def _master(salon, *, username, phone, external_id: str | None):
    """Мастер с профилем; ``external_id`` — прокси-личность бота, привязанная
    к нему (LINKED), как её оставляет ``bind_external_identity``."""
    user = User.objects.create_user(
        username=username, password="x", role="specialist", phone=phone,
    )
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.tenant = salon
    profile.timezone = "Europe/Moscow"
    profile.save()
    if external_id:
        User.objects.create(
            username=external_id, role="client", is_proxy=True, is_guest=False,
            linked_user=user,
        )
    return profile


@pytest.fixture
def master(salon):
    return _master(salon, username="wh1815_m1", phone="+79995401001", external_id="bot:max:1815001")


@pytest.fixture
def other(salon):
    return _master(salon, username="wh1815_m2", phone="+79995401002", external_id="bot:max:1815002")


@pytest.fixture
def unlinked_proxy(db) -> str:
    """Прокси, которую никто не привязывал: до M28 у неё нет профиля."""
    User.objects.create(
        username="bot:max:1815999", role="client", is_proxy=True, is_guest=False,
    )
    return "bot:max:1815999"


def _client(*, bearer: str | None = RUNTIME_TOKEN, actor: str | None = None) -> APIClient:
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _url(profile_id) -> str:
    return f"/api/v1/internal/specialists/{profile_id}/working-hours/"


def _week(*, working=(0, 1, 2, 3, 4), start="10:00", end="19:00", brk=None) -> dict:
    days = []
    for d in range(7):
        row = {"day_of_week": d, "is_working_day": d in working}
        if d in working:
            row.update({"start_time": start, "end_time": end})
            if brk:
                row.update({"break_start": brk[0], "break_end": brk[1]})
        days.append(row)
    return {"schedule": days}


def _hours(profile) -> dict[int, tuple]:
    return {
        wh.day_of_week: (wh.is_working_day, wh.start_time, wh.end_time)
        for wh in SpecialistWorkingHours.objects.filter(specialist=profile)
    }


class TestOwnProfile:
    def test_put_saves_seven_days_and_get_reads_them_back(self, master):
        api = _client(actor="bot:max:1815001")
        resp = api.put(_url(master.pk), _week(brk=("13:00", "14:00")), format="json")
        assert resp.status_code == 200, resp.content
        data = resp.data["data"]
        assert data["specialist_id"] == str(master.pk)
        assert data["timezone"] == "Europe/Moscow"
        assert len(data["schedule"]) == 7
        assert [d["is_working_day"] for d in data["schedule"]] == [True] * 5 + [False] * 2
        assert data["schedule"][0]["break_start"] == "13:00"

        saved = _hours(master)
        assert saved[0] == (True, time(10, 0), time(19, 0))
        assert saved[6][0] is False

        got = api.get(_url(master.pk))
        assert got.status_code == 200
        assert got.data["data"]["schedule"] == data["schedule"]

    def test_saved_hours_are_what_the_slot_engine_reads(self, master):
        """Та же таблица, которую читает движок слотов, — не вторая копия."""
        from appointments.models import SpecialistWorkingHours as Table

        _client(actor="bot:max:1815001").put(_url(master.pk), _week(working=(5,)), format="json")
        rows = Table.objects.filter(specialist=master, is_working_day=True)
        assert [r.day_of_week for r in rows] == [5]

    def test_get_before_any_save_is_seven_closed_days(self, master):
        got = _client(actor="bot:max:1815001").get(_url(master.pk))
        assert got.status_code == 200
        assert all(d["is_working_day"] is False for d in got.data["data"]["schedule"])


class TestSameValidation:
    def test_start_not_before_end_is_400(self, master):
        resp = _client(actor="bot:max:1815001").put(
            _url(master.pk), _week(start="19:00", end="10:00"), format="json",
        )
        assert resp.status_code == 400, resp.content
        assert _hours(master) == {}

    def test_six_days_is_400(self, master):
        body = _week()
        body["schedule"].pop()
        resp = _client(actor="bot:max:1815001").put(_url(master.pk), body, format="json")
        assert resp.status_code == 400, resp.content


class TestSameShrinkGuard:
    def test_shrinking_over_a_future_booking_is_409_and_hours_stay(self, master, salon):
        api = _client(actor="bot:max:1815001")
        assert api.put(_url(master.pk), _week(), format="json").status_code == 200

        customer = User.objects.create_user(
            username="wh1815_c", password="x", role="client", phone="+79995401003",
        )
        category = ServiceCategory.objects.create(name="WH1815", slug="wh1815-cat")
        service = Service.objects.create(
            specialist=master, category=category, name="Стрижка",
            price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        )
        # Ближайший будущий вторник, 18:00 по Москве — внутри 10–19.
        day = date.today() + timedelta(days=7)
        while day.weekday() != 1:
            day += timedelta(days=1)
        start = datetime.combine(day, time(18, 0), tzinfo=MSK)
        Appointment.objects.create(
            tenant=salon, client=customer, specialist=master, service=service,
            salon_service=None, start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            status=Appointment.Status.CONFIRMED, price=Decimal("2000.00"),
        )

        resp = api.put(_url(master.pk), _week(end="17:00"), format="json")
        assert resp.status_code == 409, resp.content
        assert resp.data["error"]["code"] == "HAS_ACTIVE_APPOINTMENTS"
        assert _hours(master)[1] == (True, time(10, 0), time(19, 0))


class TestSubject:
    def test_foreign_profile_is_403_and_untouched(self, master, other):
        resp = _client(actor="bot:max:1815001").put(_url(other.pk), _week(), format="json")
        assert resp.status_code == 403, resp.content
        assert _hours(other) == {}

    def test_unlinked_proxy_has_no_subject_yet(self, master, unlinked_proxy):
        """До M28 прокси без связи писать некуда — 403, не 404 и не запись.
        При подмене сторожа на pre-LINKED-принципал краснеет ровно этот тест."""
        resp = _client(actor=unlinked_proxy).put(_url(master.pk), _week(), format="json")
        assert resp.status_code == 403, resp.content
        assert _hours(master) == {}

    def test_unnamed_caller_is_403(self, master):
        assert _client().put(_url(master.pk), _week(), format="json").status_code == 403

    def test_provisioning_token_is_403(self, master):
        resp = _client(bearer=PROVISIONING_TOKEN, actor="bot:max:1815001").put(
            _url(master.pk), _week(), format="json",
        )
        assert resp.status_code == 403

    def test_unknown_profile_uuid_is_403_not_404(self, master):
        """Свой UUID с чужим не совпадёт — отказ до чтения строки."""
        resp = _client(actor="bot:max:1815001").get(_url(uuid.uuid4()))
        assert resp.status_code == 403
