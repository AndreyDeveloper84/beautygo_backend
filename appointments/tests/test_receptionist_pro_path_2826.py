"""DRF-2826 — the receptionist on the mobile Pro path (complete / no-show).

``appointments.authz.resolve_booking_operator`` decides in what capacity a
caller acts on a booking row. Since DRF-2826 the booking desk — an active
``admin`` OR ``receptionist`` grant in ``request.tenant`` — acts as the
salon there. These nodes pin both halves: the receptionist of THIS salon may
close a visit and mark a no-show; a receptionist of ANOTHER salon naming
this one is refused with 404 (info-hiding, as for a foreign admin in
``test_salon_ops_closure_1064``).

Fixtures are a minimal copy of DRF-1064's.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from rest_framework.test import APIClient

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment, OutboxEvent
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User

pytestmark = pytest.mark.django_db

RECEPTIONIST = TenantUserRelationship.Role.RECEPTIONIST


@pytest.fixture
def salon(db):
    return Tenant.objects.create(slug="salon-p2826", name="Pro 2826")


@pytest.fixture
def other_salon(db):
    return Tenant.objects.create(slug="other-p2826", name="Other Pro 2826")


@pytest.fixture
def specialist(salon):
    user = User.objects.create_user(
        username="p2826_spec", password="x", role="specialist", phone="+79992826001",
    )
    user.tenant = salon
    user.save(update_fields=["tenant"])
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = "2826 Master"
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.tenant = salon
    profile.save()
    return profile


@pytest.fixture
def service(specialist):
    category = ServiceCategory.objects.create(name="P2826", slug="p2826-cat")
    return Service.objects.create(
        specialist=specialist, category=category, name="2826 Service",
        price=Decimal("1000.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="p2826_client", password="x", role="client", phone="+79992826002",
    )


def _grant(user, tenant, role):
    return TenantUserRelationship.objects.create(user=user, tenant=tenant, role=role, is_active=True)


@pytest.fixture
def desk(salon):
    user = User.objects.create_user(
        username="p2826_desk", password="x", role="client", phone="+79992826003",
    )
    _grant(user, salon, RECEPTIONIST)
    return user


@pytest.fixture
def foreign_desk(salon, other_salon):
    """Receptionist of ANOTHER salon, and a plain customer of this one — so the
    request passes the membership guard and the refusal comes from the actor
    resolver, as for the foreign admin in DRF-1064."""
    user = User.objects.create_user(
        username="p2826_desk_b", password="x", role="client", phone="+79992826004",
    )
    _grant(user, other_salon, RECEPTIONIST)
    _grant(user, salon, TenantUserRelationship.Role.CUSTOMER)
    return user


def _confirmed(client_user, specialist, service) -> Appointment:
    start = (datetime.now(tz=timezone.utc) + timedelta(hours=3)).replace(second=0, microsecond=0)
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=start,
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(start_at=start, end_at=start + timedelta(hours=1)),
    )
    appt.status = Appointment.Status.CONFIRMED
    appt.save(update_fields=["status"])
    OutboxEvent.objects.all().delete()
    return appt


def _api(user, *, tenant_slug):
    client = APIClient()
    client.defaults["HTTP_X_APP_TYPE"] = "pro"
    client.defaults["HTTP_X_TENANT"] = tenant_slug
    client.force_authenticate(user=user)
    return client


class TestReceptionistOnTheProPath:
    def test_q1_own_receptionist_completes_a_visit(self, salon, desk, client_user, specialist, service):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(desk, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/complete/", {}, format="json",
        )

        assert resp.status_code == 200, resp.data

    def test_q2_own_receptionist_marks_a_no_show(self, salon, desk, client_user, specialist, service):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(desk, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/no-show/", {}, format="json",
        )

        assert resp.status_code == 200, resp.data

    @pytest.mark.parametrize("action", ["complete", "no-show"])
    def test_q3_a_receptionist_of_another_salon_is_refused(
        self, salon, foreign_desk, client_user, specialist, service, action,
    ):
        appt = _confirmed(client_user, specialist, service)

        resp = _api(foreign_desk, tenant_slug=salon.slug).post(
            f"/api/v1/appointments/{appt.id}/{action}/", {}, format="json",
        )

        assert resp.status_code in (403, 404), resp.data
        appt.refresh_from_db()
        assert appt.status == Appointment.Status.CONFIRMED
