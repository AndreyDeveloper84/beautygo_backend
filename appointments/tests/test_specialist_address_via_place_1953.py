"""DRF-1953 — адрес мастера в ответах записи берётся из места, а не из профиля (§9).

``AppointmentSpecialistSerializer.address`` был объявлен как ``CharField`` и
читал старую колонку ``SpecialistProfile.address`` — единственный объявленный
на проводе адрес мимо ``offer_address``. Ответы создания/деталей/отмены/
переноса (клиентское приложение, внутренний REST бота, консоль салона)
отдавали адрес человека.

Сторожа:

* подтверждённое место (``works_at`` → CONFIRMED) — его адрес;
* нет места / место не подтверждено — ``""``;
* старый адрес профиля заполнен в каждой фикстуре НАРОЧНО и нигде не
  всплывает (ни в поле, ни в теле ответа);
* число запросов деталей записи не растёт от того, что у мастера есть место.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from tenants.models import LocationStatus, ServiceLocation, Tenant
from users.models import SpecialistProfile, User

OLD_ADDRESS = "СТАРЫЙ адрес профиля 1953, ул. Человека, 1"
PLACE_ADDRESS = "г. Пенза, ул. Места, д. 7"


def _place(*, confirmed: bool, address: str = PLACE_ADDRESS) -> ServiceLocation:
    kwargs = dict(tenant=None, address=address, city="Пенза")
    if confirmed:
        operator = User.objects.create_user(
            username=f"op1953-{address[-3:]}", password="x",  # pragma: allowlist secret
            role="admin",
        )
        kwargs.update(
            status=LocationStatus.CONFIRMED, confirmed_by=operator,
            confirmed_at=timezone.now(), confirmed_source_ref="test-1953",
        )
    return ServiceLocation.objects.create(**kwargs)


def _profile_with_old_address(profile: SpecialistProfile, place: ServiceLocation | None) -> None:
    SpecialistProfile.objects.filter(pk=profile.pk).update(address=OLD_ADDRESS, works_at=place)


def _detail(client_user, appointment):
    c = APIClient()
    c.defaults["HTTP_X_APP_TYPE"] = "client"
    c.force_authenticate(user=client_user)
    return c.get(f"/api/v1/appointments/{appointment.id}/")


# ---------------------------------------------------------------------------
# Детали записи — клиентское приложение
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAppointmentDetailAddress:
    def test_confirmed_place_address_is_shown_not_the_profile_address(
        self, client_user, specialist, appointment,
    ):
        _profile_with_old_address(specialist, _place(confirmed=True))

        r = _detail(client_user, appointment)

        assert r.status_code == 200, r.data
        assert r.data["data"]["specialist"]["address"] == PLACE_ADDRESS
        assert OLD_ADDRESS not in r.content.decode("utf-8")

    def test_no_place_is_empty_and_the_profile_address_never_leaks(
        self, client_user, specialist, appointment,
    ):
        _profile_with_old_address(specialist, None)

        r = _detail(client_user, appointment)

        assert r.status_code == 200, r.data
        assert r.data["data"]["specialist"]["address"] == ""
        assert OLD_ADDRESS not in r.content.decode("utf-8")

    def test_unconfirmed_place_is_empty(self, client_user, specialist, appointment):
        _profile_with_old_address(specialist, _place(confirmed=False))

        r = _detail(client_user, appointment)

        assert r.status_code == 200, r.data
        assert r.data["data"]["specialist"]["address"] == ""
        assert PLACE_ADDRESS not in r.content.decode("utf-8")

    def test_query_count_does_not_grow_with_a_place(self, client_user, specialist, appointment):
        _profile_with_old_address(specialist, None)
        with CaptureQueriesContext(connection) as without_place:
            assert _detail(client_user, appointment).status_code == 200

        _profile_with_old_address(specialist, _place(confirmed=True))
        with CaptureQueriesContext(connection) as with_place:
            assert _detail(client_user, appointment).status_code == 200

        assert len(with_place.captured_queries) == len(without_place.captured_queries), (
            [q["sql"][:120] for q in with_place.captured_queries]
        )


# ---------------------------------------------------------------------------
# Создание записи — внутренний REST бота (ответ 201 тем же сериализатором)
# ---------------------------------------------------------------------------

VALID_TOKEN = "test-ayla-internal-token-1953"  # pragma: allowlist secret
EXTERNAL_USER_ID = "bot:1953"
CREATE_URL = "/api/v1/internal/appointments/"


def _future_iso(hours: int = 3) -> str:
    dt = (datetime.now(tz=dt_timezone.utc) + timedelta(hours=hours)).replace(second=0, microsecond=0)
    return dt.replace(minute=dt.minute - (dt.minute % 30)).isoformat()


@pytest.fixture
def internal_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def bot_customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x",  # pragma: allowlist secret
        role="client", is_proxy=True,
    )


@pytest.fixture
def salon_specialist(db):
    from services.models import SalonService, ServiceCategory, SpecialistService

    tenant = Tenant.objects.create(slug="i1953-t", name="Internal 1953 Tenant")
    u = User.objects.create_user(
        username="i1953_spec", password="x",  # pragma: allowlist secret
        role="specialist",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = "Internal Spec 1953"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    category = ServiceCategory.objects.create(name="I1953 Cat", slug="i1953-cat")
    salon_service = SalonService.objects.create(
        tenant=tenant, category=category, name="Internal Service 1953",
        duration_minutes=60, base_price=Decimal("1500.00"), is_active=True,
        requires_health_check=False,
    )
    SpecialistService.objects.create(
        salon_service=salon_service, specialist=p, duration_minutes=60,
        price=Decimal("1500.00"), buffer_after_minutes=0, is_active=True,
    )
    return p, salon_service


@pytest.mark.django_db
class TestInternalCreateAddress:
    def test_create_201_carries_the_confirmed_place_address(
        self, internal_token, bot_customer, salon_specialist,
    ):
        profile, salon_service = salon_specialist
        _profile_with_old_address(profile, _place(confirmed=True))
        c = APIClient()
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID

        r = c.post(CREATE_URL, {
            "client_id": str(bot_customer.id),
            "specialist_id": str(profile.id),
            "service_id": str(salon_service.id),
            "start_datetime": _future_iso(3),
        }, format="json")

        assert r.status_code == 201, r.data
        assert r.data["data"]["specialist"]["address"] == PLACE_ADDRESS
        assert OLD_ADDRESS not in r.content.decode("utf-8")
