"""Отзыв из бота под субъектом клиента (DRF-1855, карта кабинета K12).

Что заперто:

- свой завершённый визит → 201, отзыв с услугой из брони, рейтинг мастера
  пересчитан; строка журнала §96 записана;
- те же отказы, что у клиентской двери (один сервис): не завершён → 400,
  повтор → 409, чужая бронь → 404 (неотличима от несуществующей);
- чужой субъект в URL → 403 до записи; без ``X-External-User-ID`` → 403;
- положительная стража: клиентская дверь ``POST /api/v1/reviews/`` ведёт
  себя как прежде (тот же сервис).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from appointments.models import Appointment
from privacy_audit.models import PersonalDataAccessLog
from reviews.models import Review
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

pytestmark = pytest.mark.django_db

RUNTIME_TOKEN = "test-runtime-internal-token-1855"  # noqa: S105


@pytest.fixture(autouse=True)
def _tokens(settings):
    settings.AYLA_INTERNAL_API_TOKEN = RUNTIME_TOKEN


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="rv1855-t", name="Review Salon")


def _client_with_identity(nick: str, external_id: str) -> User:
    real = User.objects.create_user(
        username=nick, password="x", role="client", phone=f"+7999185{nick[-4:]}",
    )
    User.objects.create(
        username=external_id, role="client", is_proxy=True, is_guest=False, linked_user=real,
    )
    return real


@pytest.fixture
def alice(db):
    return _client_with_identity("alice1855", "bot:max:1855001")


@pytest.fixture
def bob(db):
    return _client_with_identity("bobby1856", "bot:max:1855002")


@pytest.fixture
def master(db, salon):
    u = User.objects.create_user(
        username="rv1855_master", password="x", role="specialist", phone="+79991855900",
    )
    u.tenant = salon
    u.save(update_fields=["tenant"])
    p = SpecialistProfile.objects.get(user=u)
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.tenant = salon
    p.save()
    return p


@pytest.fixture
def service(master):
    category = ServiceCategory.objects.create(name="RV1855", slug="rv1855-cat")
    return Service.objects.create(
        specialist=master, category=category, name="Массаж",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
    )


def _visit(salon, client, master, service, *, status=Appointment.Status.COMPLETED):
    start = datetime.now(tz=dt_timezone.utc) - timedelta(days=1)
    return Appointment.objects.create(
        tenant=salon, client=client, specialist=master, service=service,
        salon_service=None, start_datetime=start, end_datetime=start + timedelta(hours=1),
        status=status, price=Decimal("2000.00"),
    )


def _api(actor: str | None) -> APIClient:
    c = APIClient()
    c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {RUNTIME_TOKEN}"
    if actor is not None:
        c.defaults["HTTP_X_EXTERNAL_USER_ID"] = actor
    return c


def _url(user_id) -> str:
    return f"/api/v1/internal/users/{user_id}/reviews/"


def _post(api, user, appointment, **extra):
    return api.post(
        _url(user.pk), {"appointment_id": str(appointment.id), "rating": 5, **extra}, format="json",
    )


class TestOwnVisit:
    def test_created_with_the_bookings_service_and_rating_recalculated(
        self, alice, master, service, salon
    ):
        visit = _visit(salon, alice, master, service)

        resp = _post(_api("bot:max:1855001"), alice, visit, text="Спасибо!")

        assert resp.status_code == 201, resp.data
        review = Review.objects.get(appointment=visit)
        assert review.client_id == alice.pk
        assert review.service_id == service.pk
        assert review.text == "Спасибо!"
        master.refresh_from_db()
        assert master.reviews_count == 1
        assert float(master.rating) == 5.0

    def test_the_access_is_journaled(self, alice, master, service, salon):
        visit = _visit(salon, alice, master, service)
        before = PersonalDataAccessLog.objects.count()

        _post(_api("bot:max:1855001"), alice, visit)

        row = PersonalDataAccessLog.objects.order_by("-occurred_at").first()
        assert PersonalDataAccessLog.objects.count() == before + 1
        assert row.object_category == PersonalDataAccessLog.ObjectCategory.REVIEW
        assert row.operation == PersonalDataAccessLog.Operation.REVIEW_CREATE
        assert row.result == PersonalDataAccessLog.Result.ALLOWED


class TestSameRefusalsAsTheAppDoor:
    def test_not_completed_is_400(self, alice, master, service, salon):
        visit = _visit(salon, alice, master, service, status=Appointment.Status.CONFIRMED)
        resp = _post(_api("bot:max:1855001"), alice, visit)
        assert resp.status_code == 400
        assert not Review.objects.exists()

    def test_second_review_is_409(self, alice, master, service, salon):
        visit = _visit(salon, alice, master, service)
        api = _api("bot:max:1855001")
        assert _post(api, alice, visit).status_code == 201
        assert _post(api, alice, visit).status_code == 409
        assert Review.objects.count() == 1

    def test_someone_elses_booking_is_404(self, alice, bob, master, service, salon):
        bobs_visit = _visit(salon, bob, master, service)
        resp = _post(_api("bot:max:1855001"), alice, bobs_visit)
        assert resp.status_code == 404
        assert not Review.objects.exists()


class TestSubject:
    def test_a_foreign_subject_is_403_and_writes_nothing(self, alice, bob, master, service, salon):
        bobs_visit = _visit(salon, bob, master, service)
        resp = _post(_api("bot:max:1855001"), bob, bobs_visit)
        assert resp.status_code == 403
        assert not Review.objects.exists()

    def test_an_unnamed_caller_is_403(self, alice, master, service, salon):
        visit = _visit(salon, alice, master, service)
        assert _post(_api(None), alice, visit).status_code == 403
        assert not Review.objects.exists()


class TestAppDoorUnchanged:
    def test_client_app_still_creates_through_the_same_rules(self, alice, master, service, salon):
        visit = _visit(salon, alice, master, service)
        api = APIClient()
        api.defaults["HTTP_X_APP_TYPE"] = "client"
        api.force_authenticate(user=alice)

        resp = api.post("/api/v1/reviews/", {"appointment_id": str(visit.id), "rating": 4}, format="json")

        assert resp.status_code == 201, resp.data
        assert api.post(
            "/api/v1/reviews/", {"appointment_id": str(visit.id), "rating": 4}, format="json",
        ).status_code == 409
