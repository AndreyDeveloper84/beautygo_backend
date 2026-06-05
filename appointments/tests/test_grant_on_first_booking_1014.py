"""Grant-on-first-booking — single platform-wide rule (#1014).

Pins the generalised grant in
``CreateBookingService._execute_atomic``: ANY successful booking
through ANY channel grants ``TUR(client, specialist.tenant)`` the
first time the client books in that tenant — no longer gated on the
cross-tenant ``request_tenant_id`` condition.

Hard constraints under test:
- create-path only (grant happens inside the booking transaction);
- F2 preserved — a revoked TUR refuses the booking (404), never a
  silent re-grant (#152);
- idempotent — repeat bookings don't duplicate the TUR;
- TOCTOU-safe — concurrent first-bookings produce exactly ONE TUR
  (``select_for_update`` on the TUR rows, #154).

The cross-tenant headline + repeat + cross-tenant-revoke cases live in
``test_cross_tenant_create_404.py``; this file pins the NEW same-tenant /
no-tenant-context behaviour that #1014 introduces.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from django.db import connection

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.models import Appointment
from rest_framework.exceptions import NotFound
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User


def _future_utc(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _make_specialist(tenant, *, suffix: str) -> SpecialistProfile:
    u = User.objects.create_user(
        username=f"g1014_spec_{suffix}", password="x", role="specialist",
        phone=f"+799914{suffix}",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.tenant = tenant
    p.display_name = f"Spec {suffix}"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


def _make_service(specialist) -> Service:
    cat, _ = ServiceCategory.objects.get_or_create(
        slug="g1014-cat", defaults={"name": "G1014 Cat"},
    )
    return Service.objects.create(
        specialist=specialist, category=cat, name="G1014 Service",
        price=Decimal("1500.00"), duration_minutes=60, is_active=True,
        buffer_after_minutes=0,
    )


def _dto(client, specialist, service, *, hours: int = 3) -> CreateBookingDTO:
    return CreateBookingDTO(
        client_id=client.id,
        specialist_id=specialist.id,
        service_id=service.id,
        start_at=_future_utc(hours),
        idempotency_key=str(uuid4()),
        # No tenant context — the nationwide-bot / header-less mobile case.
        request_tenant_id=None,
    )


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="g1014-t", name="G1014 Tenant")


@pytest.fixture
def fresh_client(db):
    """Customer with NO tenant set → no User.post_save bridge TUR.

    The clean slate that proves the grant fires on first booking even
    without any tenant context on the request.
    """
    return User.objects.create_user(
        username="g1014_client", password="x", role="client",
        phone="+79991400000",
    )


@pytest.mark.django_db
class TestPlatformWideGrant:
    def test_first_booking_no_tenant_context_grants_tur(
        self, fresh_client, tenant,
    ):
        """No request_tenant_id, client has no prior TUR → booking
        grants TUR(client, specialist.tenant). This is the rule the
        nationwide bot relies on."""
        specialist = _make_specialist(tenant, suffix="01")
        service = _make_service(specialist)

        assert not TenantUserRelationship.objects.filter(
            user=fresh_client, tenant=tenant,
        ).exists()

        CreateBookingService().execute(_dto(fresh_client, specialist, service))

        granted = TenantUserRelationship.objects.filter(
            user=fresh_client, tenant=tenant, is_active=True,
        )
        assert granted.count() == 1
        assert granted.first().role == TenantUserRelationship.Role.CUSTOMER

    def test_repeat_booking_does_not_duplicate_tur(
        self, fresh_client, tenant,
    ):
        specialist = _make_specialist(tenant, suffix="02")
        service = _make_service(specialist)

        CreateBookingService().execute(
            _dto(fresh_client, specialist, service, hours=3))
        CreateBookingService().execute(
            _dto(fresh_client, specialist, service, hours=5))

        assert TenantUserRelationship.objects.filter(
            user=fresh_client, tenant=tenant, is_active=True,
        ).count() == 1

    def test_revoked_tur_refuses_booking_same_tenant(
        self, fresh_client, tenant,
    ):
        """F2 (#152) preserved under the universal rule: a revoked
        relationship refuses the booking with 404 — a banned customer
        cannot silently re-book via ANY channel, including same-tenant.
        No appointment, no re-grant."""
        from django.utils import timezone as dj_tz
        specialist = _make_specialist(tenant, suffix="03")
        service = _make_service(specialist)
        TenantUserRelationship.objects.create(
            user=fresh_client, tenant=tenant, is_active=False,
            revoked_at=dj_tz.now(), revoke_reason="admin_ban",
        )

        with pytest.raises(NotFound):
            CreateBookingService().execute(
                _dto(fresh_client, specialist, service))

        assert Appointment.objects.count() == 0
        assert not TenantUserRelationship.objects.filter(
            user=fresh_client, tenant=tenant, is_active=True,
        ).exists()

    def test_grant_only_on_create_not_on_failed_booking(
        self, fresh_client, tenant,
    ):
        """Grant is inside the booking transaction — a slot conflict
        rolls the TUR write back with the booking (no orphan grant)."""
        specialist = _make_specialist(tenant, suffix="04")
        service = _make_service(specialist)
        # First booking holds the slot AND grants the TUR.
        first = _dto(fresh_client, specialist, service, hours=4)
        CreateBookingService().execute(first)
        TenantUserRelationship.objects.filter(
            user=fresh_client, tenant=tenant,
        ).delete()  # wipe so we can prove the 2nd (failing) one doesn't grant

        # Second booking: a different fresh client collides on the same slot.
        other = User.objects.create_user(
            username="g1014_other", password="x", role="client",
            phone="+79991400099",
        )
        clashing = CreateBookingDTO(
            client_id=other.id, specialist_id=specialist.id,
            service_id=service.id, start_at=first.start_at,
            idempotency_key=str(uuid4()), request_tenant_id=None,
        )
        from appointments.domain.exceptions import SlotNotAvailableError
        with pytest.raises(SlotNotAvailableError):
            CreateBookingService().execute(clashing)
        # The failed booking left no TUR for `other`.
        assert not TenantUserRelationship.objects.filter(
            user=other, tenant=tenant,
        ).exists()


@pytest.mark.django_db(transaction=True)
class TestGrantConcurrency:
    """TOCTOU: two concurrent first-bookings for the same (client,
    tenant) at DISTINCT slots must yield exactly ONE active TUR. The
    ``select_for_update`` lock on the TUR rows serialises the grant."""

    def test_concurrent_first_bookings_grant_single_tur(self):
        tenant = Tenant.objects.create(slug="g1014-race", name="Race")
        specialist = _make_specialist(tenant, suffix="55")
        service = _make_service(specialist)
        client = User.objects.create_user(
            username="g1014_race_client", password="x", role="client",
            phone="+79991405555",
        )

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def book(hours: int):
            barrier.wait()  # maximise overlap on the TUR lock
            try:
                CreateBookingService().execute(
                    _dto(client, specialist, service, hours=hours))
            except Exception as exc:  # noqa: BLE001 — surfaced via list
                errors.append(exc)
            finally:
                connection.close()

        t1 = threading.Thread(target=book, args=(3,))
        t2 = threading.Thread(target=book, args=(6,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert errors == [], f"unexpected booking errors: {errors}"
        assert Appointment.objects.filter(
            specialist=specialist, client=client,
        ).count() == 2
        assert TenantUserRelationship.objects.filter(
            user=client, tenant=tenant, is_active=True,
        ).count() == 1
        # pytest-django flushes the DB after a transaction=True test, so
        # no manual teardown is needed.
