"""Tests for #246 Q2 specialist-departure refund cascade.

Founder Q2 ack 2026-05-26:
    Cancellation = no-fault refund к customer (full refund, no penalty)
    Customer gets push «Запись cancelled, salon обещает связаться»

The cascade is triggered from `revoke_tenant_user_relationship` when
the revoked TUR has `role=staff`. End-to-end the flow is:

  admin revokes specialist TUR
    → cascade_specialist_departure runs
    → CancelBookingService.execute(initiator='system',
                                    reason='specialist_departure',
                                    policy=ForceFullRefundCancellationPolicy)
      for each active future booking
    → booking.cancelled outbox event fires per booking
    → notification handler branches on reason → uses
      'appointment_cancelled_specialist_departure' template
    → for each PAID Payment: YooKassa refund + Payment.status=REFUNDED

§H.3 surface — cascade affects multiple users' financial state.
Adversarial focus on:
- Only future bookings of the SAME (specialist, tenant) pair are
  touched. Other specialists' bookings, other tenants' bookings,
  past bookings — must remain intact.
- Customer revoke (role=customer) does NOT trigger cascade.
- YooKassa failure does not roll back the booking cancel.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment, OutboxEvent
from payments.models import Payment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, TenantUserRelationship, User
from users.services import (
    cascade_specialist_departure,
    revoke_tenant_user_relationship,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant_a(db):
    return Tenant.objects.create(slug="q2-a", name="Q2 Tenant A")


@pytest.fixture
def tenant_b(db):
    return Tenant.objects.create(slug="q2-b", name="Q2 Tenant B")


@pytest.fixture
def admin_user(db, tenant_a):
    u = User.objects.create_user(
        username="q2_admin", password="x", role="admin",
        phone="+79992000001",
    )
    TenantUserRelationship.objects.filter(user=u).delete()
    TenantUserRelationship.objects.create(
        user=u, tenant=tenant_a,
        role=TenantUserRelationship.Role.ADMIN,
    )
    return u


def _make_specialist(tenant, *, suffix: str, name: str):
    user = User.objects.create_user(
        username=f"q2_spec_{suffix}", password="x", role="specialist",
        phone=f"+79992{suffix}",
    )
    profile = SpecialistProfile.objects.get(user=user)
    profile.display_name = name
    profile.tenant = tenant
    profile.status = SpecialistProfile.ProfileStatus.ACTIVE
    profile.is_available = True
    profile.is_booking_enabled = True
    profile.timezone = "Europe/Moscow"
    profile.save()
    # Staff TUR so cascade is triggered on revoke.
    TenantUserRelationship.objects.filter(user=user).delete()
    TenantUserRelationship.objects.create(
        user=user, tenant=tenant,
        role=TenantUserRelationship.Role.STAFF,
    )
    return user, profile


@pytest.fixture
def specialist_a(db, tenant_a):
    return _make_specialist(tenant_a, suffix="100010", name="Olya")


@pytest.fixture
def specialist_b(db, tenant_a):
    return _make_specialist(tenant_a, suffix="100020", name="Marina")


@pytest.fixture
def specialist_in_b(db, tenant_b):
    return _make_specialist(tenant_b, suffix="100030", name="OlyaInB")


@pytest.fixture
def customer(db, tenant_a):
    u = User.objects.create_user(
        username="q2_customer", password="x", role="client",
        phone="+79992200001",
    )
    TenantUserRelationship.objects.filter(user=u).delete()
    TenantUserRelationship.objects.create(
        user=u, tenant=tenant_a,
        role=TenantUserRelationship.Role.CUSTOMER,
    )
    return u


def _make_category(db):
    return ServiceCategory.objects.create(
        name="Q2cat", slug="q2-cat",
    )


def _make_service(specialist_profile, category):
    return Service.objects.create(
        specialist=specialist_profile,
        category=category,
        name="Q2 Service",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
    )


def _future(hours: int = 3) -> datetime:
    return (
        datetime.now(tz=timezone.utc) + timedelta(hours=hours)
    ).replace(second=0, microsecond=0)


def _make_booking(
    *, client_user, specialist_profile, service, start_at,
):
    from uuid import uuid4
    dto = CreateBookingDTO(
        client_id=client_user.id,
        specialist_id=specialist_profile.id,
        service_id=service.id,
        start_at=start_at,
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist_profile, service,
        target_interval=TimeInterval(
            start_at=start_at,
            end_at=start_at + timedelta(hours=1),
        ),
    )
    return appt


# ---------------------------------------------------------------------------
# Cascade core behavior
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCascadeBookingFiltering:
    def test_only_future_active_bookings_cancelled(
        self, customer, specialist_a, tenant_a,
    ):
        """Only PENDING/AWAITING_PAYMENT/CONFIRMED bookings with
        start_at>now are cancelled. Past bookings and CANCELLED
        bookings stay as-is."""
        spec_user, spec_profile = specialist_a
        category = _make_category(specialist_a[0].__class__.objects.db)
        service = _make_service(spec_profile, category)

        future_appt = _make_booking(
            client_user=customer, specialist_profile=spec_profile,
            service=service, start_at=_future(3),
        )
        past_appt = _make_booking(
            client_user=customer, specialist_profile=spec_profile,
            service=service, start_at=_future(6),
        )
        # Backdate the second appointment so it's "past" — direct DB
        # tweak because the booking service blocks past-start input.
        past_start = datetime.now(tz=timezone.utc) - timedelta(hours=2)
        Appointment.objects.filter(id=past_appt.id).update(
            start_datetime=past_start,
            end_datetime=past_start + timedelta(hours=1),
        )

        summary = cascade_specialist_departure(
            specialist_user=spec_user,
            tenant=tenant_a,
            actor=None,
        )

        future_appt.refresh_from_db()
        past_appt.refresh_from_db()
        assert future_appt.status == "cancelled"
        # Past appointment untouched.
        assert past_appt.status != "cancelled"
        assert summary["cancelled_count"] == 1

    def test_other_tenants_bookings_not_touched(
        self, customer, specialist_a, specialist_in_b,
        tenant_a, tenant_b,
    ):
        """Cascade is tenant-scoped — bookings on a different tenant
        (even by the same specialist user) stay intact."""
        spec_user_a, spec_profile_a = specialist_a
        spec_user_b, spec_profile_b = specialist_in_b
        cat_a = ServiceCategory.objects.create(slug="q2-cat-a", name="A")
        cat_b = ServiceCategory.objects.create(slug="q2-cat-b", name="B")
        svc_a = _make_service(spec_profile_a, cat_a)
        svc_b = _make_service(spec_profile_b, cat_b)

        booking_a = _make_booking(
            client_user=customer, specialist_profile=spec_profile_a,
            service=svc_a, start_at=_future(3),
        )
        booking_b = _make_booking(
            client_user=customer, specialist_profile=spec_profile_b,
            service=svc_b, start_at=_future(4),
        )

        cascade_specialist_departure(
            specialist_user=spec_user_a,
            tenant=tenant_a,
            actor=None,
        )

        booking_a.refresh_from_db()
        booking_b.refresh_from_db()
        assert booking_a.status == "cancelled"
        # specialist_in_b's booking in tenant_b — untouched.
        assert booking_b.status != "cancelled"

    def test_other_specialists_bookings_not_touched(
        self, customer, specialist_a, specialist_b, tenant_a,
    ):
        """Cascade specialist-scoped — sibling specialist in the SAME
        tenant keeps their bookings."""
        spec_user_a, spec_profile_a = specialist_a
        spec_user_b, spec_profile_b = specialist_b
        cat = _make_category(spec_user_a.__class__.objects.db)
        svc_a = _make_service(spec_profile_a, cat)
        svc_b = _make_service(spec_profile_b, cat)
        booking_a = _make_booking(
            client_user=customer, specialist_profile=spec_profile_a,
            service=svc_a, start_at=_future(3),
        )
        booking_b = _make_booking(
            client_user=customer, specialist_profile=spec_profile_b,
            service=svc_b, start_at=_future(5),
        )

        cascade_specialist_departure(
            specialist_user=spec_user_a,
            tenant=tenant_a,
            actor=None,
        )

        booking_a.refresh_from_db()
        booking_b.refresh_from_db()
        assert booking_a.status == "cancelled"
        assert booking_b.status != "cancelled"


# ---------------------------------------------------------------------------
# Outbox event + reason carry
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestCascadeOutboxAndReason:
    def test_booking_cancelled_event_carries_specialist_departure_reason(
        self, customer, specialist_a, tenant_a,
    ):
        spec_user, spec_profile = specialist_a
        cat = _make_category(spec_user.__class__.objects.db)
        svc = _make_service(spec_profile, cat)
        _make_booking(
            client_user=customer, specialist_profile=spec_profile,
            service=svc, start_at=_future(3),
        )

        OutboxEvent.objects.filter(
            topic="booking.cancelled",
        ).delete()

        cascade_specialist_departure(
            specialist_user=spec_user,
            tenant=tenant_a,
            actor=None,
        )

        events = OutboxEvent.objects.filter(topic="booking.cancelled")
        assert events.count() == 1
        ev = events.get()
        assert ev.payload["data"]["reason"] == "specialist_departure"
        assert ev.payload["data"]["refund_percent"] == 100.0
        assert ev.payload["data"]["initiator_role"] == "system"

    def test_refund_percent_always_100_regardless_of_window(
        self, customer, specialist_a, tenant_a,
    ):
        """Standard policy would charge a fee for cancellation <24h
        from start. ForceFullRefundCancellationPolicy overrides — the
        customer pays nothing because the master left, not them."""
        spec_user, spec_profile = specialist_a
        cat = _make_category(spec_user.__class__.objects.db)
        svc = _make_service(spec_profile, cat)
        # Booking in 1 hour — standard policy would refund 0%.
        _make_booking(
            client_user=customer, specialist_profile=spec_profile,
            service=svc, start_at=_future(1),
        )

        OutboxEvent.objects.filter(topic="booking.cancelled").delete()

        cascade_specialist_departure(
            specialist_user=spec_user,
            tenant=tenant_a,
            actor=None,
        )

        ev = OutboxEvent.objects.filter(topic="booking.cancelled").get()
        assert ev.payload["data"]["refund_percent"] == 100.0


# ---------------------------------------------------------------------------
# Revoke integration — cascade fires only for STAFF
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRevokeIntegration:
    def test_customer_revoke_does_not_cascade_bookings(
        self, admin_user, customer, specialist_a, tenant_a,
    ):
        """Revoking a CUSTOMER's TUR must NOT touch booking state.
        Cascade is staff-only."""
        spec_user, spec_profile = specialist_a
        cat = _make_category(spec_user.__class__.objects.db)
        svc = _make_service(spec_profile, cat)
        booking = _make_booking(
            client_user=customer, specialist_profile=spec_profile,
            service=svc, start_at=_future(3),
        )

        revoke_tenant_user_relationship(
            target_user=customer, tenant=tenant_a, actor=admin_user,
            reason="kicked", notify_customer=False,
        )

        booking.refresh_from_db()
        assert booking.status != "cancelled"

    def test_staff_revoke_triggers_cascade(
        self, admin_user, customer, specialist_a, tenant_a,
    ):
        spec_user, spec_profile = specialist_a
        cat = _make_category(spec_user.__class__.objects.db)
        svc = _make_service(spec_profile, cat)
        booking = _make_booking(
            client_user=customer, specialist_profile=spec_profile,
            service=svc, start_at=_future(3),
        )

        revoke_tenant_user_relationship(
            target_user=spec_user, tenant=tenant_a, actor=admin_user,
            reason="master_left", notify_customer=False,
        )

        booking.refresh_from_db()
        assert booking.status == "cancelled"


# ---------------------------------------------------------------------------
# YooKassa refund best-effort + tagged failure
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestRefundBestEffort:
    def _seed_paid_payment(self, appointment):
        return Payment.objects.create(
            appointment=appointment,
            amount=appointment.price,
            status=Payment.Status.PAID,
            specialist_income=Decimal("1380.00"),
            platform_fee=Decimal("120.00"),
            provider="yookassa",
            provider_payment_id="yk-paid-q2-001",
            provider_client_secret="https://yookassa/old",
        )

    def test_refund_marks_payment_refunded_on_success(
        self, customer, specialist_a, tenant_a,
    ):
        spec_user, spec_profile = specialist_a
        cat = _make_category(spec_user.__class__.objects.db)
        svc = _make_service(spec_profile, cat)
        appt = _make_booking(
            client_user=customer, specialist_profile=spec_profile,
            service=svc, start_at=_future(3),
        )
        payment = self._seed_paid_payment(appt)

        with patch(
            "payments.services.YooKassaService",
        ) as mock_yk_cls:
            mock_yk_cls.return_value.refund_payment = MagicMock(
                return_value={"refund_id": "rf-1", "status": "succeeded"},
            )
            summary = cascade_specialist_departure(
                specialist_user=spec_user,
                tenant=tenant_a,
                actor=None,
            )

        payment.refresh_from_db()
        assert payment.status == Payment.Status.REFUNDED
        assert summary["refunded_count"] == 1
        assert summary["refund_failures"] == []

    def test_yookassa_failure_does_not_rollback_cancel(
        self, customer, specialist_a, tenant_a,
    ):
        """If YooKassa refund_payment raises, the booking stays
        cancelled. The Payment row stays PAID and the booking_id is
        tagged in summary['refund_failures'] for ops follow-up."""
        from payments.exceptions import PaymentClientError

        spec_user, spec_profile = specialist_a
        cat = _make_category(spec_user.__class__.objects.db)
        svc = _make_service(spec_profile, cat)
        appt = _make_booking(
            client_user=customer, specialist_profile=spec_profile,
            service=svc, start_at=_future(3),
        )
        payment = self._seed_paid_payment(appt)

        with patch(
            "payments.services.YooKassaService",
        ) as mock_yk_cls:
            mock_yk_cls.return_value.refund_payment = MagicMock(
                side_effect=PaymentClientError("FCM down"),
            )
            summary = cascade_specialist_departure(
                specialist_user=spec_user,
                tenant=tenant_a,
                actor=None,
            )

        appt.refresh_from_db()
        payment.refresh_from_db()
        # Booking cancelled regardless of refund outcome.
        assert appt.status == "cancelled"
        assert payment.status == Payment.Status.PAID  # NOT REFUNDED
        assert str(appt.id) in summary["refund_failures"]
