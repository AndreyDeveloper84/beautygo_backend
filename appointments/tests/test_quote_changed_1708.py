"""DRF-1708 (B-6.2) — цена и длительность на подтверждении сверяются с применяемыми.

До этого среза подтверждение записи показывало цену и длительность,
а сервис создания брал текущие значения услуги, ничего не сверяя:
человек видел одно, применялось другое, и никто об этом не узнавал.

Контракт: ``POST /api/v1/internal/appointments/`` принимает
необязательные ``quoted_price`` / ``quoted_duration_minutes``. Без них —
поведение прежнее (положительная стража: старые вызывающие ничего не
сверяют и не ломаются). С ними — сверка **внутри транзакции создания,
до записи**; расхождение → ``409 QUOTE_CHANGED``,
``details = {field, quoted, applied}``, записи нет.

Один код на оба поля (ayla-3b, 12.09): клиенту в любом случае
показывать обе пары и переспрашивать; какое поле — ``details.field``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from services.models import SalonService, ServiceCategory, SpecialistService
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-1708"
EXTERNAL_USER_ID = "bot:quote-1708"
INTERNAL_CREATE_URL = "/api/v1/internal/appointments/"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username=EXTERNAL_USER_ID, password="x", role="client",
        phone="+79991708001", is_proxy=True,
    )


@pytest.fixture
def specialist(db):
    u = User.objects.create_user(
        username="quote_spec", password="x", role="specialist",
        phone="+79991708002",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "Quote Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="Quote Cat", slug="quote-cat")


@pytest.fixture
def service(specialist, category):
    tenant_id = SpecialistProfile.objects.values_list("tenant_id", flat=True).get(pk=specialist.pk)
    salon_service = SalonService.objects.create(
        tenant_id=tenant_id,
        category=category,
        name="Quote Service",
        duration_minutes=60,
        base_price=Decimal("1500.00"),
        is_active=True,
        requires_health_check=False,
    )
    SpecialistService.objects.create(
        salon_service=salon_service,
        specialist=specialist,
        duration_minutes=60,
        price=Decimal("1500.00"),
        buffer_after_minutes=0,
        is_active=True,
    )
    return salon_service


def _future_iso(hours: int = 3) -> str:
    return (datetime.now(tz=timezone.utc) + timedelta(hours=hours)).replace(
        second=0, microsecond=0
    ).isoformat()


def _api() -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {VALID_TOKEN}"
    c.defaults["HTTP_X_EXTERNAL_USER_ID"] = EXTERNAL_USER_ID
    return c


def _body(customer, specialist, service, **extra) -> dict:
    return {
        "client_id": str(customer.id),
        "specialist_id": str(specialist.id),
        "service_id": str(service.id),
        "start_datetime": _future_iso(3),
        "payment_required": False,
        **extra,
    }


@pytest.mark.django_db
class TestQuoteMatchesOrIsAbsent:
    def test_without_quote_creates_as_before(self, customer, specialist, service):
        """Положительная стража: старый вызывающий ничего не сверяет."""
        r = _api().post(INTERNAL_CREATE_URL, _body(customer, specialist, service), format="json")
        assert r.status_code == 201, r.content
        assert Appointment.objects.count() == 1

    def test_matching_quote_creates(self, customer, specialist, service):
        r = _api().post(
            INTERNAL_CREATE_URL,
            _body(customer, specialist, service, quoted_price="1500.00", quoted_duration_minutes=60),
            format="json",
        )
        assert r.status_code == 201, r.content
        assert Appointment.objects.count() == 1

    def test_price_compared_by_value_not_by_spelling(self, customer, specialist, service):
        """«1500» и «1500.00» — одна цена; формат строки на клиенте не расхождение.

        Через сервис, не через ручку: сериализатор сам нормализует «1500» в
        Decimal("1500.00"), и проба на ручке зеленела бы даже при сравнении
        строк в сервисе. Здесь сервис получает Decimal("1500") напрямую.
        """
        from datetime import datetime, timedelta, timezone as tz
        from uuid import uuid4

        from appointments.application.dto import CreateBookingDTO
        from appointments.application.services.create_booking_service import CreateBookingService

        dto = CreateBookingDTO(
            client_id=customer.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=(datetime.now(tz=tz.utc) + timedelta(hours=3)).replace(second=0, microsecond=0),
            idempotency_key=str(uuid4()),
            payment_required=False,
            confirm_immediately=True,
            quoted_price=Decimal("1500"),
        )
        result = CreateBookingService().execute(dto)
        assert result.booking_id is not None
        assert Appointment.objects.count() == 1


@pytest.mark.django_db
class TestQuoteChangedRefuses:
    def test_price_changed_is_409_with_both_values_and_no_row(self, customer, specialist, service):
        r = _api().post(
            INTERNAL_CREATE_URL,
            _body(customer, specialist, service, quoted_price="1200.00", quoted_duration_minutes=60),
            format="json",
        )
        assert r.status_code == 409, r.content
        err = r.json()["error"]
        assert err["code"] == "QUOTE_CHANGED"
        assert err["details"] == {"field": "price", "quoted": "1200.00", "applied": "1500.00"}
        # Внутри транзакции, до записи: строки нет.
        assert Appointment.objects.count() == 0

    def test_duration_changed_is_409_with_its_own_field(self, customer, specialist, service):
        r = _api().post(
            INTERNAL_CREATE_URL,
            _body(customer, specialist, service, quoted_price="1500.00", quoted_duration_minutes=45),
            format="json",
        )
        assert r.status_code == 409, r.content
        err = r.json()["error"]
        assert err["code"] == "QUOTE_CHANGED"
        assert err["details"] == {"field": "duration_minutes", "quoted": 45, "applied": 60}
        assert Appointment.objects.count() == 0

    def test_price_is_checked_before_duration(self, customer, specialist, service):
        """Оба разошлись — человеку первым называется расхождение в деньгах."""
        r = _api().post(
            INTERNAL_CREATE_URL,
            _body(customer, specialist, service, quoted_price="1200.00", quoted_duration_minutes=45),
            format="json",
        )
        assert r.status_code == 409
        assert r.json()["error"]["details"]["field"] == "price"

    def test_quote_is_checked_against_what_applies_now(self, customer, specialist, service):
        """Цена изменилась ПОСЛЕ показа: то, что применилось бы, — новое значение."""
        SpecialistService.objects.filter(salon_service=service, specialist=specialist).update(
            price=Decimal("1700.00")
        )
        r = _api().post(
            INTERNAL_CREATE_URL,
            _body(customer, specialist, service, quoted_price="1500.00"),
            format="json",
        )
        assert r.status_code == 409, r.content
        assert r.json()["error"]["details"] == {"field": "price", "quoted": "1500.00", "applied": "1700.00"}
        assert Appointment.objects.count() == 0

    def test_negative_quote_is_validation_not_quote_changed(self, customer, specialist, service):
        """Ошибка ввода — VALIDATION_ERROR, а не «изменилось»: разные адреса починки."""
        r = _api().post(
            INTERNAL_CREATE_URL,
            _body(customer, specialist, service, quoted_price="-1"),
            format="json",
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"
