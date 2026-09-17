"""Формы админки для закрытий салона и исключений в графике мастера (раздел Q; аудит 15.09).

До правки ``TenantClosure`` и ``SpecialistScheduleException`` в админке не были
зарегистрированы. Единственный писатель — API админа салона, и он охраняет
брони:
* закрыть дату, на которую есть живая запись, — 409 ``HAS_ACTIVE_APPOINTMENTS``;
  закрытие салона спрашивает каждого мастера салона в его часовом поясе;
* сократить часы рабочего дня так, что запись выпадает из рамки, — отказ,
  запись откатывается.

Форма админки в обход этих сторожей оставила бы клиента с записью без мастера
молча. Решение главного окна (вариант A): формы применяют ТЕ ЖЕ сторожа.
Правил два не бывает: счёт живых записей в окне — одна функция в
``schedule_impact_service`` для API и для формы.

Проверяется:
* отказ — ошибка формы с числом записей, ничего не записано, нигде нет 500;
* окно — локальный день мастера (``local_day_window_utc``), как в API: мастер
  в другом часовом поясе;
* уникальность даты, порядок времени, перерыв вне смены, времена у выходного —
  ошибки формы;
* кэш слотов: созданное или удалённое в админке сразу видно в слотах.
  Сначала слоты читаются и ложатся в кэш, потом правка в админке, потом
  повторное чтение: без сброса кэша по сигналу второе чтение отдало бы
  старое.

Клиент не пробрасывает исключение запроса: тест видит настоящий статус.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from appointments.application.dto import GetAvailabilityDTO
from appointments.application.services.availability_query_service import (
    AvailabilityQueryService,
)
from appointments.models import (
    Appointment,
    SpecialistScheduleException,
    SpecialistWorkingHours,
    TenantClosure,
)
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

MSK = "Europe/Moscow"
YEKT = "Asia/Yekaterinburg"  # UTC+5, на два часа впереди Москвы

CLOSURE_ADD = "admin:appointments_tenantclosure_add"
CLOSURE_DELETE = "admin:appointments_tenantclosure_delete"
EXCEPTION_ADD = "admin:appointments_specialistscheduleexception_add"


def _thursday_ahead(weeks: int = 2) -> date:
    base = date.today() + timedelta(weeks=weeks)
    return base + timedelta(days=(3 - base.weekday()) % 7)


TARGET = _thursday_ahead()


@pytest.fixture(autouse=True)
def _fresh_slot_cache():
    # LocMemCache живёт весь процесс: без очистки слоты одного теста
    # отвечали бы другому.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="schedule-forms-salon", name="Салон графика")


def _make_master(salon, *, username: str, phone: str, tz: str = MSK) -> SpecialistProfile:
    user = User.objects.create_user(
        username=username, password="x",  # pragma: allowlist secret
        role="specialist", phone=phone,
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = username
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.tenant = salon
    profile.timezone = tz
    profile.save()
    for day in range(7):
        working = day < 5
        SpecialistWorkingHours.objects.create(
            specialist=profile, day_of_week=day, is_working_day=working,
            start_time=time(10, 0) if working else None,
            end_time=time(19, 0) if working else None,
            break_start=time(13, 0) if working else None,
            break_end=time(14, 0) if working else None,
        )
    return profile


@pytest.fixture
def master(salon):
    return _make_master(salon, username="schedule-forms-master", phone="+79991979001")


@pytest.fixture
def east_master(salon):
    return _make_master(salon, username="schedule-forms-east", phone="+79991979002", tz=YEKT)


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="schedule-forms-client", password="x",  # pragma: allowlist secret
        role="client", phone="+79991979003",
    )


def _service(master) -> Service:
    category, _ = ServiceCategory.objects.get_or_create(
        slug="schedule-forms-cat", defaults={"name": "Schedule forms"},
    )
    return Service.objects.create(
        specialist=master, category=category, name="Стрижка",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


def _booking(salon, customer, master, *, at_local: time, on: date = TARGET) -> Appointment:
    """Запись по местным часам мастера — сторож переводит именно их."""
    start = datetime.combine(on, at_local, tzinfo=ZoneInfo(master.timezone))
    return Appointment.objects.create(
        tenant=salon, client=customer, specialist=master, service=_service(master),
        salon_service=None, start_datetime=start, end_datetime=start + timedelta(hours=1),
        status=Appointment.Status.CONFIRMED, price=Decimal("2000.00"),
    )


def _slots(master, service, day: date = TARGET) -> list[str]:
    result = AvailabilityQueryService().get_day_availability(
        GetAvailabilityDTO(specialist_id=master.id, target_date=day, service_id=service.id)
    )
    if not result.is_working_day:
        return []
    return [s.start_local for s in result.slots]


@pytest.fixture
def owner(db):
    return User.objects.create_superuser(
        username="schedule-forms-owner", password="pw",  # pragma: allowlist secret
        email="schedule-forms@example.com", role="admin",
    )


def _client(owner) -> Client:
    c = Client(raise_request_exception=False)
    c.force_login(owner)
    return c


def _closure_body(salon, *, on: date = TARGET, start: str = "", end: str = "") -> dict:
    return {"tenant": str(salon.pk), "date": on.isoformat(), "start_time": start, "end_time": end, "reason": ""}


def _exception_body(master, *, working: bool, start="", end="", break_start="", break_end="", on=TARGET) -> dict:
    body = {
        "specialist": str(master.pk), "date": on.isoformat(),
        "start_time": start, "end_time": end, "break_start": break_start, "break_end": break_end, "note": "",
    }
    if working:
        body["is_working_day"] = "on"
    return body


def _form_errors(response) -> str:
    form = response.context["adminform"].form
    return str(form.errors)


# ---------------------------------------------------------------------------
# Закрытие салона
# ---------------------------------------------------------------------------


class TestClosureForm:
    def test_the_add_page_exists(self, owner):
        page = _client(owner).get(reverse(CLOSURE_ADD))
        assert page.status_code == 200, page.status_code

    def test_a_full_day_closure_without_bookings_saves_and_empties_the_slots(self, owner, salon, master):
        service = _service(master)
        assert _slots(master, service), "precondition: a working Thursday has slots (and they are now cached)"
        r = _client(owner).post(reverse(CLOSURE_ADD), _closure_body(salon))
        assert r.status_code == 302, r.status_code
        assert TenantClosure.objects.filter(tenant=salon, date=TARGET).count() == 1
        assert _slots(master, service) == [], "a closure made in the admin must reach the slots at once"

    def test_a_closure_over_a_live_booking_is_a_form_error_with_the_count(self, owner, salon, master, customer):
        _booking(salon, customer, master, at_local=time(15, 0))
        r = _client(owner).post(reverse(CLOSURE_ADD), _closure_body(salon))
        assert r.status_code == 200, r.status_code
        assert "1" in _form_errors(r), _form_errors(r)
        assert not TenantClosure.objects.filter(tenant=salon, date=TARGET).exists()

    def test_the_window_is_the_masters_local_day_booking_inside_is_refused(
        self, owner, salon, east_master, customer,
    ):
        # 00:30 в Екатеринбурге на TARGET — это 22:30 по Москве накануне:
        # окно по московскому дню его бы пропустило.
        _booking(salon, customer, east_master, at_local=time(0, 30))
        r = _client(owner).post(reverse(CLOSURE_ADD), _closure_body(salon))
        assert r.status_code == 200, r.status_code
        assert not TenantClosure.objects.filter(tenant=salon, date=TARGET).exists()

    def test_the_window_is_the_masters_local_day_booking_outside_is_allowed(
        self, owner, salon, east_master, customer,
    ):
        # 01:30 в Екатеринбурге на следующий день — это 23:30 по Москве на
        # TARGET: окно по московскому дню отказало бы зря.
        _booking(salon, customer, east_master, at_local=time(1, 30), on=TARGET + timedelta(days=1))
        r = _client(owner).post(reverse(CLOSURE_ADD), _closure_body(salon))
        assert r.status_code == 302, (r.status_code, _form_errors(r) if r.context else None)
        assert TenantClosure.objects.filter(tenant=salon, date=TARGET).exists()

    def test_a_second_full_day_closure_for_the_same_date_is_a_form_error(self, owner, salon):
        TenantClosure.objects.create(tenant=salon, date=TARGET)
        r = _client(owner).post(reverse(CLOSURE_ADD), _closure_body(salon))
        assert r.status_code == 200, r.status_code
        assert _form_errors(r)
        assert TenantClosure.objects.filter(tenant=salon, date=TARGET).count() == 1

    def test_end_before_start_is_a_form_error(self, owner, salon):
        r = _client(owner).post(reverse(CLOSURE_ADD), _closure_body(salon, start="15:00", end="12:00"))
        assert r.status_code == 200, r.status_code
        assert "end_time" in r.context["adminform"].form.errors
        assert not TenantClosure.objects.filter(tenant=salon).exists()

    def test_a_start_without_an_end_is_a_form_error_not_a_500(self, owner, salon):
        r = _client(owner).post(reverse(CLOSURE_ADD), _closure_body(salon, start="12:00"))
        assert r.status_code == 200, r.status_code
        assert _form_errors(r)
        assert not TenantClosure.objects.filter(tenant=salon).exists()

    def test_deleting_a_closure_in_the_admin_opens_the_slots(self, owner, salon, master):
        service = _service(master)
        closure = TenantClosure.objects.create(tenant=salon, date=TARGET)
        assert _slots(master, service) == [], "precondition: closed day, and the empty answer is now cached"
        r = _client(owner).post(reverse(CLOSURE_DELETE, args=[closure.pk]), {"post": "yes"})
        assert r.status_code == 302, r.status_code
        assert not TenantClosure.objects.filter(pk=closure.pk).exists()
        assert _slots(master, service), "a closure deleted in the admin must reopen the slots at once"


# ---------------------------------------------------------------------------
# Исключение в графике мастера
# ---------------------------------------------------------------------------


class TestScheduleExceptionForm:
    def test_a_day_off_over_a_live_booking_is_a_form_error(self, owner, salon, master, customer):
        _booking(salon, customer, master, at_local=time(15, 0))
        r = _client(owner).post(reverse(EXCEPTION_ADD), _exception_body(master, working=False))
        assert r.status_code == 200, r.status_code
        assert "1" in _form_errors(r), _form_errors(r)
        assert not SpecialistScheduleException.objects.filter(specialist=master, date=TARGET).exists()

    def test_shorter_hours_that_strand_a_booking_are_a_form_error(self, owner, salon, master, customer):
        # Запись 15:00–16:00 влезает в неделю (10–19, перерыв 13–14) и не
        # влезает в «работаю 10–13».
        _booking(salon, customer, master, at_local=time(15, 0))
        r = _client(owner).post(
            reverse(EXCEPTION_ADD), _exception_body(master, working=True, start="10:00", end="13:00"),
        )
        assert r.status_code == 200, r.status_code
        assert _form_errors(r)
        assert not SpecialistScheduleException.objects.filter(specialist=master, date=TARGET).exists()

    def test_a_second_exception_for_the_same_master_and_date_is_a_form_error(self, owner, master):
        SpecialistScheduleException.objects.create(specialist=master, date=TARGET, is_working_day=False)
        r = _client(owner).post(
            reverse(EXCEPTION_ADD), _exception_body(master, working=True, start="10:00", end="19:00"),
        )
        assert r.status_code == 200, r.status_code
        assert _form_errors(r)
        assert SpecialistScheduleException.objects.filter(specialist=master, date=TARGET).count() == 1

    def test_a_break_outside_the_shift_is_a_form_error(self, owner, master):
        r = _client(owner).post(reverse(EXCEPTION_ADD), _exception_body(
            master, working=True, start="10:00", end="14:00", break_start="15:00", break_end="16:00",
        ))
        assert r.status_code == 200, r.status_code
        assert _form_errors(r)
        assert not SpecialistScheduleException.objects.filter(specialist=master).exists()

    def test_a_day_off_with_times_is_a_form_error_not_a_500(self, owner, master):
        r = _client(owner).post(
            reverse(EXCEPTION_ADD), _exception_body(master, working=False, start="10:00", end="12:00"),
        )
        assert r.status_code == 200, r.status_code
        assert _form_errors(r)
        assert not SpecialistScheduleException.objects.filter(specialist=master).exists()

    def test_a_valid_day_off_without_bookings_saves_and_empties_the_slots(self, owner, master):
        service = _service(master)
        assert _slots(master, service), "precondition: slots exist and are cached"
        r = _client(owner).post(reverse(EXCEPTION_ADD), _exception_body(master, working=False))
        assert r.status_code == 302, (r.status_code, _form_errors(r) if r.context else None)
        assert SpecialistScheduleException.objects.filter(specialist=master, date=TARGET).exists()
        assert _slots(master, service) == [], "a day off made in the admin must reach the slots at once"
