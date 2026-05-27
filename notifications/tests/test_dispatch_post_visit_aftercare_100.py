"""Tests for B9 post-visit aftercare beat (task #100).

Founder pilot safety rule (memory project_pilot_scope_discipline):
NO LLM-generated care advice — ONLY approved canonical text per
service. Schema enforces this via Service.aftercare_text default=''
+ filter that excludes empty-string services.

Coverage:
- Service WITH aftercare_text → push queued at T+2h
- Service WITHOUT aftercare_text → silently skipped (default safety)
- Beat re-run after queue → idempotent (no double push)
- Outside the window (T-2h fresh / T+2h+30min stale) → not picked up
- Refunded payment → suppressed
- Partially-refunded payment → suppressed
- Non-COMPLETED status → not picked up
- Push body carries the verbatim aftercare_text (no embellishment)
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.value_objects import TimeInterval
from appointments.models import Appointment
from notifications.models import Notification
from notifications.tasks import (
    AFTERCARE_TEMPLATE_ID,
    dispatch_post_visit_aftercare,
)
from payments.models import Payment
from services.models import Service, ServiceCategory
from tenants.models import Tenant
from users.models import SpecialistProfile, User


APPROVED_TEXT = "Не мочить кутикулу 2 часа. Питательный крем 2 раза в день."


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tenant(db):
    return Tenant.objects.create(slug="b9", name="B9 Tenant")


@pytest.fixture
def customer(db):
    return User.objects.create_user(
        username="b9_client", password="x", role="client",
        phone="+79995000001",
    )


@pytest.fixture
def specialist_profile(db, tenant):
    user = User.objects.create_user(
        username="b9_spec", password="x", role="specialist",
        phone="+79995000010",
    )
    p = SpecialistProfile.objects.get(user=user)
    p.display_name = "Мастер"
    p.tenant = tenant
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.is_available = True
    p.is_booking_enabled = True
    p.timezone = "Europe/Moscow"
    p.save()
    return p


@pytest.fixture
def category(db):
    return ServiceCategory.objects.create(name="B9cat", slug="b9-cat")


@pytest.fixture
def service_with_aftercare(specialist_profile, category):
    return Service.objects.create(
        specialist=specialist_profile,
        category=category,
        name="With Aftercare",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
        aftercare_text=APPROVED_TEXT,
    )


@pytest.fixture
def service_without_aftercare(specialist_profile, category):
    return Service.objects.create(
        specialist=specialist_profile,
        category=category,
        name="No Aftercare",
        price=Decimal("1500.00"),
        duration_minutes=60,
        is_active=True,
        buffer_after_minutes=0,
        # aftercare_text NOT set — default '' suppresses push.
    )


def _make_completed_booking(
    customer, specialist_profile, service, *, end_at,
):
    """Create a booking via the canonical service path then flip it to
    COMPLETED with the requested end_datetime (used to land in or near
    the T+2h aftercare window)."""
    from uuid import uuid4
    start_at = end_at - timedelta(hours=1)
    # Future stamp first so the booking-window policy accepts; we then
    # rewrite the timestamps directly.
    dto = CreateBookingDTO(
        client_id=customer.id,
        specialist_id=specialist_profile.id,
        service_id=service.id,
        start_at=timezone.now() + timedelta(hours=4),
        idempotency_key=str(uuid4()),
    )
    appt, _ = CreateBookingService()._execute_atomic(
        dto, specialist_profile, service,
        target_interval=TimeInterval(
            start_at=dto.start_at,
            end_at=dto.start_at + timedelta(hours=1),
        ),
    )
    Appointment.objects.filter(id=appt.id).update(
        start_datetime=start_at,
        end_datetime=end_at,
        status=Appointment.Status.COMPLETED,
    )
    appt.refresh_from_db()
    return appt


def _in_window_end_datetime():
    """A datetime that lands inside the [now-2h30m, now-2h] beat window."""
    return timezone.now() - timedelta(hours=2, minutes=15)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAftercareHappyPath:
    def test_service_with_aftercare_text_queues_push(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        appt = _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=_in_window_end_datetime(),
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 1
        notif = Notification.objects.get(
            user=customer, template_id=AFTERCARE_TEMPLATE_ID,
        )
        assert notif.data["appointment_id"] == str(appt.id)
        # Body carries the SERVICE's approved text VERBATIM — no
        # template-side embellishment. Locks the contract; if a
        # future template change adds a static prefix this fails.
        assert notif.body == APPROVED_TEXT

    def test_idempotent_across_beat_runs(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=_in_window_end_datetime(),
        )

        first = dispatch_post_visit_aftercare()
        second = dispatch_post_visit_aftercare()

        assert first["queued"] == 1
        assert second["queued"] == 0
        assert second["skipped"] == 1
        # Exactly one Notification row total.
        assert Notification.objects.filter(
            user=customer, template_id=AFTERCARE_TEMPLATE_ID,
        ).count() == 1


# ---------------------------------------------------------------------------
# Safety filter — service without approved text
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAftercareSafetyFilter:
    def test_service_without_aftercare_text_silently_skipped(
        self, customer, specialist_profile, service_without_aftercare,
    ):
        _make_completed_booking(
            customer, specialist_profile, service_without_aftercare,
            end_at=_in_window_end_datetime(),
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 0
        # No Notification row created — silent suppression.
        assert Notification.objects.filter(
            user=customer, template_id=AFTERCARE_TEMPLATE_ID,
        ).count() == 0

    def test_whitespace_only_aftercare_text_silently_skipped(
        self, customer, specialist_profile, category,
    ):
        """Reviewer MUST_FIX (a65c36ddf6347a2cc): curator accidentally
        saving '   ' or '\\n' must not produce a blank push body. The
        beat's regex-based filter rejects anything that strips to
        empty."""
        svc = Service.objects.create(
            specialist=specialist_profile,
            category=category,
            name="Whitespace Aftercare",
            price=Decimal("1500.00"),
            duration_minutes=60,
            is_active=True,
            buffer_after_minutes=0,
            aftercare_text="   \n  \t  ",
        )
        _make_completed_booking(
            customer, specialist_profile, svc,
            end_at=_in_window_end_datetime(),
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 0
        assert Notification.objects.filter(
            user=customer, template_id=AFTERCARE_TEMPLATE_ID,
        ).count() == 0

    def test_curly_brace_in_aftercare_text_does_not_poison_batch(
        self, customer, specialist_profile, category,
    ):
        """Reviewer MUST_FIX (a65c36ddf6347a2cc): if a curator types
        literal `{` in approved Russian text (e.g. 'Через {2} часа'),
        ``str.format`` raises mid-render. The beat must catch + log +
        skip — not abort the whole batch. A good row in the SAME tick
        must still receive its push."""
        # Bad row — curator typed unescaped curly braces.
        bad_svc = Service.objects.create(
            specialist=specialist_profile,
            category=category,
            name="Bad Curly",
            price=Decimal("1500.00"),
            duration_minutes=60,
            is_active=True,
            buffer_after_minutes=0,
            aftercare_text="Через {2} часа смазать",
        )
        bad_appt = _make_completed_booking(
            customer, specialist_profile, bad_svc,
            end_at=_in_window_end_datetime(),
        )

        # Good row in the SAME beat window.
        good_svc = Service.objects.create(
            specialist=specialist_profile,
            category=category,
            name="Good Plain",
            price=Decimal("1500.00"),
            duration_minutes=60,
            is_active=True,
            buffer_after_minutes=0,
            aftercare_text=APPROVED_TEXT,
        )
        good_customer = User.objects.create_user(
            username="b9_good_customer", password="x", role="client",
            phone="+79995000111",
        )
        good_appt = _make_completed_booking(
            good_customer, specialist_profile, good_svc,
            end_at=_in_window_end_datetime() - timedelta(minutes=2),
        )

        # Beat runs, hits bad row first OR second — order doesn't
        # matter for the contract.
        result = dispatch_post_visit_aftercare()

        # Good row got its push despite the bad row in the same tick.
        good_notif = Notification.objects.filter(
            user=good_customer, template_id=AFTERCARE_TEMPLATE_ID,
            data__appointment_id=str(good_appt.id),
        )
        assert good_notif.count() == 1
        # Bad row produced no push.
        bad_notif = Notification.objects.filter(
            template_id=AFTERCARE_TEMPLATE_ID,
            data__appointment_id=str(bad_appt.id),
        )
        assert bad_notif.count() == 0
        # Counter reflects: 1 queued (good), 0 explicit-skip.
        assert result["queued"] == 1


# ---------------------------------------------------------------------------
# Time-window filter
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAftercareWindow:
    def test_too_fresh_appointment_not_picked_up(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        # Ended 30min ago — far below 2h lag.
        _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=timezone.now() - timedelta(minutes=30),
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 0

    def test_too_old_appointment_not_picked_up(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        # Ended 4h ago — past the T+2h window's upper bound.
        _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=timezone.now() - timedelta(hours=4),
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 0


# ---------------------------------------------------------------------------
# Status filter — only COMPLETED triggers
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAftercareStatusFilter:
    def test_cancelled_appointment_not_picked_up(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        appt = _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=_in_window_end_datetime(),
        )
        # Flip COMPLETED → CANCELLED. Real flow uses CancelBookingService;
        # direct update is the test shortcut.
        Appointment.objects.filter(id=appt.id).update(
            status=Appointment.Status.CANCELLED,
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 0

    def test_no_show_appointment_not_picked_up(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        appt = _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=_in_window_end_datetime(),
        )
        Appointment.objects.filter(id=appt.id).update(
            status=Appointment.Status.NO_SHOW,
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 0


# ---------------------------------------------------------------------------
# Refund suppression
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestAftercareRefundSuppression:
    def test_fully_refunded_payment_suppresses_push(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        appt = _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=_in_window_end_datetime(),
        )
        Payment.objects.create(
            appointment=appt,
            amount=Decimal("1500.00"),
            refunded_amount=Decimal("1500.00"),
            status=Payment.Status.REFUNDED,
            specialist_income=Decimal("1380.00"),
            platform_fee=Decimal("120.00"),
            provider="yookassa",
            provider_payment_id="yk-b9-full-refund",
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 0
        assert result["suppressed_refund"] == 1

    def test_partially_refunded_payment_suppresses_push(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        appt = _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=_in_window_end_datetime(),
        )
        Payment.objects.create(
            appointment=appt,
            amount=Decimal("1500.00"),
            refunded_amount=Decimal("500.00"),
            status=Payment.Status.PARTIALLY_REFUNDED,
            specialist_income=Decimal("1380.00"),
            platform_fee=Decimal("120.00"),
            provider="yookassa",
            provider_payment_id="yk-b9-partial-refund",
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 0
        assert result["suppressed_refund"] == 1

    def test_paid_unrefunded_payment_does_not_suppress(
        self, customer, specialist_profile, service_with_aftercare,
    ):
        appt = _make_completed_booking(
            customer, specialist_profile, service_with_aftercare,
            end_at=_in_window_end_datetime(),
        )
        Payment.objects.create(
            appointment=appt,
            amount=Decimal("1500.00"),
            status=Payment.Status.PAID,
            specialist_income=Decimal("1380.00"),
            platform_fee=Decimal("120.00"),
            provider="yookassa",
            provider_payment_id="yk-b9-paid",
        )

        result = dispatch_post_visit_aftercare()

        assert result["queued"] == 1
        assert result["suppressed_refund"] == 0
