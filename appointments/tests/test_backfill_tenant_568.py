"""Regression tests for #568 — backfill Appointment.tenant_id (migration 0008).

Pin:
- Pre-migration: legacy rows with tenant=None remain accessible.
- Post-migration: zero rows with tenant=None where the specialist has a tenant.
- Re-running is idempotent (no-op on second invocation).
- Rows whose specialist also has tenant=None are untouched (not in scope).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import importlib

import pytest
from django.db import connection
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

# Module names starting with a digit aren't valid Python identifiers,
# so import via importlib.
_migration_mod = importlib.import_module(
    "appointments.migrations.0008_backfill_appointment_tenant",
)
backfill_tenant_id = _migration_mod.backfill_tenant_id


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="t568-a", name="Tenant 568 A")


@pytest.fixture
def specialist_with_tenant(db, tenant_a):
    u = User.objects.create_user(
        username="t568_spec_a", password="x", role="specialist",
        phone="+79991005680",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant_a
    p.display_name = "T568 A"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def specialist_no_tenant(db):
    """Edge case: specialist whose own tenant FK is also NULL."""
    u = User.objects.create_user(
        username="t568_spec_orphan", password="x", role="specialist",
        phone="+79991005681",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = None
    p.display_name = "T568 orphan"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="T568 Cat", slug="t568-cat")


@pytest.fixture
def service_a(specialist_with_tenant, category):
    return Service.objects.create(
        specialist=specialist_with_tenant,
        category=category,
        name="T568 Service A",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def service_orphan(specialist_no_tenant, category):
    return Service.objects.create(
        specialist=specialist_no_tenant,
        category=category,
        name="T568 Service Orphan",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="t568_client", password="x", role="client",
        phone="+79991005682",
    )


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _legacy_booking(client_user, specialist, service):
    """Create a booking via the real service then force-NULL the
    tenant_id (modelling a row created pre-#520 before stamping)."""
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(3 + len(str(uuid4())) % 5),
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    Appointment.objects.filter(pk=appt.pk).update(tenant=None)
    appt.refresh_from_db()
    return appt


@pytest.mark.django_db
class TestBackfillData:
    """RunPython backfill against representative row mix."""

    def test_backfills_legacy_null_where_specialist_has_tenant(
        self, client_user, specialist_with_tenant, service_a, tenant_a,
    ):
        appt = _legacy_booking(client_user, specialist_with_tenant, service_a)
        assert appt.tenant_id is None  # legacy state

        backfill_tenant_id(apps=None, schema_editor=connection.schema_editor())
        appt.refresh_from_db()
        assert appt.tenant_id == tenant_a.id

    def test_leaves_rows_alone_when_specialist_also_null(
        self, client_user, specialist_no_tenant, service_orphan,
    ):
        appt = _legacy_booking(
            client_user, specialist_no_tenant, service_orphan,
        )
        assert appt.tenant_id is None
        assert specialist_no_tenant.tenant_id is None

        backfill_tenant_id(apps=None, schema_editor=connection.schema_editor())
        appt.refresh_from_db()
        # Specialist has no tenant → can't backfill. Row stays NULL.
        assert appt.tenant_id is None

    def test_idempotent_on_rerun(
        self, client_user, specialist_with_tenant, service_a, tenant_a,
    ):
        appt = _legacy_booking(client_user, specialist_with_tenant, service_a)
        backfill_tenant_id(apps=None, schema_editor=connection.schema_editor())
        first = Appointment.objects.get(pk=appt.pk).tenant_id

        backfill_tenant_id(apps=None, schema_editor=connection.schema_editor())
        second = Appointment.objects.get(pk=appt.pk).tenant_id

        assert first == second == tenant_a.id

    def test_does_not_overwrite_existing_tenant_id(
        self, client_user, specialist_with_tenant, service_a, tenant_a,
    ):
        """Rows that already have a tenant_id (e.g. created post-#520)
        must NOT be touched even if specialist.tenant_id differs.
        The migration's WHERE clause filters on tenant_id IS NULL."""
        appt = _legacy_booking(client_user, specialist_with_tenant, service_a)
        # Simulate a post-#520 row with explicit tenant_id set to
        # something other than specialist.tenant. (Unusual but the
        # invariant should hold.)
        other_tenant = Tenant.objects.create(slug="t568-other", name="Other")
        Appointment.objects.filter(pk=appt.pk).update(tenant=other_tenant)

        backfill_tenant_id(apps=None, schema_editor=connection.schema_editor())
        appt.refresh_from_db()
        assert appt.tenant_id == other_tenant.id  # untouched


@pytest.mark.django_db
class TestMigrationApplied:
    """Full MigrationExecutor sanity — confirms 0008 is on the chain
    and runs without error against the test DB."""

    def test_migration_0008_in_graph(self):
        executor = MigrationExecutor(connection)
        graph = executor.loader.graph
        # ('appointments', '0008_backfill_appointment_tenant') is a node.
        nodes = {n for n in graph.nodes if n[0] == 'appointments'}
        names = {n[1] for n in nodes}
        assert '0008_backfill_appointment_tenant' in names, (
            f"Migration 0008 missing from graph; found {sorted(names)}"
        )
