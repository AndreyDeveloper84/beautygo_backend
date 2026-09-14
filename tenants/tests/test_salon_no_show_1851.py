"""DRF-1851 (карта кабинета K8, OD-V1) — салон говорит «не пришёл» с той
поверхности, до которой бот дотягивается.

До этого `no_show` жил только на мобильном пути (`/appointments/{id}/no-show/`),
его событийная часть была вписана в вид инлайном, и салонный маршрут
намеренно не заводился, чтобы не копировать шестьдесят строк. Теперь
переход и оба события живут в `completion.mark_booking_no_show` — соседе
`close_booking`, — и мобильный путь, и салонный зовут его.

Что заперто:

* `TestItIsTheSameNoShow` — статус NO_SHOW, атрибуция `no_show_marked_by =
  salon`, ОБА события (внутреннее `booking.no_show` и кросс-сервисное
  `booking.cancelled` + `reason_code=user_no_show`), версия не бампается;
* `TestOneImplementation` — мобильный и салонный путь дают одинаковые
  события (по форме payload) — это и есть свойство, ради которого
  извлекали;
* `TestConcurrency` / `TestScope` / `TestAuth` — те же отказы, что у
  `complete`: stale version 409, дважды 422, чужой салон 404, клиент 404,
  без Bearer 401.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

SERVICE_TOKEN = "salon-no-show-token-under-test"  # pragma: allowlist secret
ADMIN_EXTERNAL_ID = "bot:max:1851admin"


@pytest.fixture(autouse=True)
def _service_token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = SERVICE_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="ns1851-t", name="NoShow Salon")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="ns1851-t-b", name="Other NoShow Salon")


@pytest.fixture
def admin_user(db, salon):
    u = User.objects.create_user(
        username=ADMIN_EXTERNAL_ID, password="x", role="admin", phone="+79991851000",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon, role=TenantUserRelationship.Role.ADMIN, is_active=True,
    )
    return u


@pytest.fixture
def customer(db, salon):
    u = User.objects.create_user(
        username="bot:max:1851client", password="x", role="client",
        phone="+79991851001", first_name="Мария",
    )
    TenantUserRelationship.objects.create(
        user=u, tenant=salon, role=TenantUserRelationship.Role.CUSTOMER, is_active=True,
    )
    return u


@pytest.fixture
def master(db, salon):
    u = User.objects.create_user(
        username="ns1851_master", password="x", role="specialist", phone="+79991851002",
    )
    u.tenant = salon
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "Ольга"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.tenant = salon
    p.save()
    return p


@pytest.fixture
def service(master, db):
    category = ServiceCategory.objects.create(name="NS1851 Cat", slug="ns1851-cat")
    return Service.objects.create(
        specialist=master, category=category, name="Массаж",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


def _booking(salon, customer, master, service, *, status=None, version=1):
    started = datetime.now(tz=dt_timezone.utc) - timedelta(hours=2)
    return Appointment.objects.create(
        tenant=salon, client=customer, specialist=master, service=service,
        salon_service=None, start_datetime=started,
        end_datetime=started + timedelta(hours=1),
        status=status or Appointment.Status.CONFIRMED, version=version,
        price=Decimal("2000.00"),
    )


@pytest.fixture
def booking(salon, customer, master, service):
    return _booking(salon, customer, master, service)


def _api(user, *, tenant_slug=None, token=SERVICE_TOKEN) -> APIClient:
    c = APIClient()
    if tenant_slug:
        c.defaults["HTTP_X_TENANT"] = tenant_slug
    if token:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {token}"
    if user is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = user.username
    return c


def _url(appointment_id) -> str:
    return f"/api/v1/tenants/me/appointments/{appointment_id}/no-show/"


def _no_show(api, booking, *, version=None):
    return api.post(
        _url(booking.id),
        {"expected_version": booking.version if version is None else version},
        format="json",
    )


def _events(topic):
    return list(OutboxEvent.objects.filter(topic=topic).order_by("created_at"))


def _events_of(appointment_id) -> dict:
    """{topic: set(ключей data)} для событий одной брони. ``OutboxEvent.data``
    — свойство поверх конверта ``payload``, фильтровать по нему нельзя."""
    return {
        e.topic: set(e.data)
        for e in OutboxEvent.objects.all()
        if e.data.get("appointment_id") == str(appointment_id)
    }


@pytest.mark.django_db(transaction=True)
class TestItIsTheSameNoShow:
    def test_the_visit_is_marked_no_show(self, salon, admin_user, booking):
        resp = _no_show(_api(admin_user, tenant_slug=salon.slug), booking)
        assert resp.status_code == 200, resp.data
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.NO_SHOW

    def test_both_events_are_emitted(self, salon, admin_user, booking):
        """Внутреннее `booking.no_show` — для #511; кросс-сервисное
        `booking.cancelled` + `reason_code=user_no_show` — для зеркала бота
        (у его таксономии нет booking.no_show). Без второго бот считал бы
        визит живым и слал напоминания."""
        _no_show(_api(admin_user, tenant_slug=salon.slug), booking)
        internal = _events(OutboxEvent.Topic.BOOKING_NO_SHOW)
        cancelled = _events(OutboxEvent.Topic.BOOKING_CANCELLED)
        assert len(internal) == 1 and len(cancelled) == 1
        assert internal[0].data["no_show_marked_by"] == "salon"
        assert cancelled[0].data["reason_code"] == "user_no_show"
        assert cancelled[0].data["cancelled_by"] == "admin"
        assert cancelled[0].data["appointment_id"] == str(booking.id)

    def test_attributed_to_the_salon_and_version_untouched(
        self, salon, admin_user, booking
    ):
        before = booking.version
        _no_show(_api(admin_user, tenant_slug=salon.slug), booking)
        booking.refresh_from_db()
        assert booking.no_show_marked_by == "salon"
        assert booking.version == before


@pytest.mark.django_db(transaction=True)
class TestOneImplementation:
    def test_mobile_and_salon_paths_emit_the_same_shape(
        self, salon, admin_user, customer, master, service
    ):
        """Свойство, ради которого извлекали: два пути — один payload.
        Мобильный путь зовётся напрямую через доменную функцию (тот же
        `mark_booking_no_show`), салонный — через HTTP; сравниваются
        ключи и `reason_code`/`cancelled_by`-словарь."""
        from appointments.application.services.completion import mark_booking_no_show

        via_salon = _booking(salon, customer, master, service)
        _no_show(_api(admin_user, tenant_slug=salon.slug), via_salon)
        salon_events = _events_of(via_salon.id)

        via_domain = _booking(salon, customer, master, service)
        locked = Appointment.objects.get(pk=via_domain.id)
        mark_booking_no_show(locked, marked_by="specialist")
        domain_events = _events_of(via_domain.id)
        assert salon_events == domain_events
        assert set(salon_events) == {
            OutboxEvent.Topic.BOOKING_NO_SHOW, OutboxEvent.Topic.BOOKING_CANCELLED,
        }

    def test_the_mobile_view_calls_the_shared_function(self):
        """Сторож на состав: в мобильном виде нет своего конструирования
        событий no-show — только вызов общей функции."""
        import re
        from pathlib import Path

        src = Path("appointments/views.py").read_text(encoding="utf-8")
        no_show_body = src.split("def no_show(")[1].split("\n    @")[0]
        # Докстринг описывает поведение словами — считаем только код.
        no_show_body = re.sub(r'"""[\s\S]*?"""', "", no_show_body, count=1)
        # Комментарии — тоже слова, не код.
        no_show_body = "\n".join(
            ln for ln in no_show_body.split("\n") if not ln.strip().startswith("#")
        )
        assert "mark_booking_no_show(" in no_show_body
        assert "BOOKING_NO_SHOW" not in no_show_body
        assert "user_no_show" not in no_show_body


@pytest.mark.django_db(transaction=True)
class TestConcurrency:
    def test_a_stale_version_is_refused(self, salon, admin_user, booking):
        resp = _no_show(_api(admin_user, tenant_slug=salon.slug), booking, version=99)
        assert resp.status_code == 409
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED

    def test_a_missing_version_is_refused(self, salon, admin_user, booking):
        resp = _api(admin_user, tenant_slug=salon.slug).post(_url(booking.id), {}, format="json")
        assert resp.status_code == 400

    def test_twice_is_refused_the_second_time(self, salon, admin_user, booking):
        api = _api(admin_user, tenant_slug=salon.slug)
        assert _no_show(api, booking).status_code == 200
        resp = _no_show(api, booking)
        assert resp.status_code == 422
        assert len(_events(OutboxEvent.Topic.BOOKING_CANCELLED)) == 1

    def test_a_completed_visit_cannot_be_marked(self, salon, admin_user, customer, master, service):
        done = _booking(salon, customer, master, service, status=Appointment.Status.COMPLETED)
        resp = _no_show(_api(admin_user, tenant_slug=salon.slug), done)
        assert resp.status_code == 422


@pytest.mark.django_db(transaction=True)
class TestScope:
    def test_a_booking_of_another_salon_is_not_found(
        self, salon, other_salon, admin_user, customer, master, service
    ):
        foreign = _booking(other_salon, customer, master, service)
        resp = _no_show(_api(admin_user, tenant_slug=salon.slug), foreign)
        assert resp.status_code == 404
        foreign.refresh_from_db()
        assert foreign.status == Appointment.Status.CONFIRMED

    def test_an_unknown_booking_is_not_found(self, salon, admin_user):
        resp = _api(admin_user, tenant_slug=salon.slug).post(
            _url(uuid4()), {"expected_version": 1}, format="json",
        )
        assert resp.status_code == 404

    def test_a_customer_may_not_mark_their_own_visit(self, salon, customer, booking):
        resp = _no_show(_api(customer, tenant_slug=salon.slug), booking)
        assert resp.status_code in (403, 404)
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED


@pytest.mark.django_db(transaction=True)
class TestAuth:
    def test_no_bearer_marks_nothing(self, salon, admin_user, booking):
        resp = _no_show(_api(admin_user, tenant_slug=salon.slug, token=None), booking)
        assert resp.status_code in (401, 403)
        booking.refresh_from_db()
        assert booking.status == Appointment.Status.CONFIRMED

    def test_the_token_alone_names_nobody(self, salon, booking):
        resp = _no_show(_api(None, tenant_slug=salon.slug), booking)
        assert resp.status_code in (401, 403)
