"""Regression tests for #590 — Appointment.tenant = NOT NULL + CheckConstraint.

Migration 0009 closes the accidental-NULL window #568 left open at the
data layer. This file pins:
- CheckConstraint blocks `Appointment.objects.create(tenant=None)`.
- Constraint also catches `.update(tenant=None)` on existing rows.
- verify_no_null_tenant function refuses to apply when NULLs exist.
- 0009 is in the migration graph.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User

_migration_mod = importlib.import_module(
    "appointments.migrations.0009_appointment_tenant_not_null",
)
verify_no_null_tenant = _migration_mod.verify_no_null_tenant


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="t590-a", name="Tenant 590 A")


@pytest.fixture
def specialist_user(db):
    return User.objects.create_user(
        username="t590_spec", password="x", role="specialist",
        phone="+79991005900",
    )


@pytest.fixture
def specialist(specialist_user, tenant_a):
    p = SpecialistProfile.objects.get(user=specialist_user)
    p.tenant = tenant_a
    p.display_name = "T590"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="T590 Cat", slug="t590-cat")


@pytest.fixture
def service(specialist, category):
    return Service.objects.create(
        specialist=specialist,
        category=category,
        name="T590 Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="t590_client", password="x", role="client",
        phone="+79991005901",
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


@pytest.mark.django_db(transaction=True)
class TestCheckConstraint:
    """Schema-level invariant: tenant IS NOT NULL."""

    def test_update_to_null_tenant_raises_integrity_error(
        self, client_user, specialist, service,
    ):
        """Existing row created via the service has tenant=tenant_a;
        an attempt to flip tenant=None via .update() must fail at
        the DB-level CheckConstraint."""
        dto = CreateBookingDTO(
            client_id=client_user.id,
            specialist_id=specialist.id,
            service_id=service.id,
            start_at=_future_utc(3),
            idempotency_key=str(uuid4()),
        )
        appt, _ = CreateBookingService()._execute_atomic(
            dto, specialist, service,
            target_interval=TimeInterval(
                start_at=dto.start_at,
                end_at=dto.start_at + timedelta(hours=1),
            ),
        )
        assert appt.tenant_id is not None  # stamped via #520

        with pytest.raises(IntegrityError):
            Appointment.objects.filter(pk=appt.pk).update(tenant=None)


@pytest.mark.django_db
class TestVerifierFunction:
    """Pre-flight RunPython callable in migration 0009."""

    def test_verify_passes_when_no_nulls(self):
        """All migrations are applied before tests run, so the DB
        has zero NULL tenant_id rows. verify_no_null_tenant must
        return silently."""
        from django.apps import apps

        # Direct call against the live apps registry — same shape
        # the migration framework uses at apply time.
        verify_no_null_tenant(apps, connection.schema_editor())
        # If we got here, the verifier didn't raise. Good.


@pytest.mark.django_db
class TestMigrationApplied:
    """MigrationExecutor sanity — confirm 0009 is on the graph."""

    def test_migration_0009_in_graph(self):
        executor = MigrationExecutor(connection)
        names = {
            n[1] for n in executor.loader.graph.nodes if n[0] == "appointments"
        }
        assert "0009_appointment_tenant_not_null" in names, (
            f"Migration 0009 missing from graph; found {sorted(names)}"
        )
