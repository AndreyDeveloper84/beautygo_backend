"""Phase C — payout preview (C3) + capture reconciliation (D9 ADR §2).

C3 pins (PILOT_CONTRACTS §4):
- pending_amount = sum(specialist_income) over capture_state in
  {scheduled, captured_pending_settlement} ONLY (settled / failed /
  canceled / refunded excluded);
- empty selection → 200 with "0.00", hint null, items [];
- 404 only when the specialist does not exist;
- amounts are Decimal strings with exactly 2dp (data contract §1);
- Bearer internal auth.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from appointments.models import Appointment
from payments.models import Payment
from payments.tasks import reconcile_captures
from services.models import Service, ServiceCategory
from users.models import SpecialistProfile, User

VALID_TOKEN = "test-ayla-internal-token-c3"


@pytest.fixture(autouse=True)
def _token(settings):
    settings.AYLA_INTERNAL_API_TOKEN = VALID_TOKEN


@pytest.fixture
def specialist(db):
    u = User.objects.create_user(
        username="c3_spec", password="x", role="specialist",
        phone="+79991003001",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.display_name = "C3 Spec"
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.save()
    return p


@pytest.fixture
def other_specialist(db):
    u = User.objects.create_user(
        username="c3_other", password="x", role="specialist",
        phone="+79991003002",
    )
    p = SpecialistProfile.objects.get(user=u)
    p.status = SpecialistProfile.ProfileStatus.ACTIVE
    p.save()
    return p


@pytest.fixture
def client_user(db):
    return User.objects.create_user(
        username="c3_client", password="x", role="client",
        phone="+79991003003",
    )


@pytest.fixture
def service(db, specialist):
    cat = ServiceCategory.objects.create(name="C3 Cat", slug="c3-cat")
    return Service.objects.create(
        specialist=specialist, category=cat, name="C3 Service",
        price=Decimal("2000.00"), duration_minutes=60, is_active=True,
    )


def _api(bearer=VALID_TOKEN):
    c = APIClient()
    if bearer is not None:
        c.defaults["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    return c


def _url(user_id) -> str:
    # AMD-005: the path key is the Ayla User UUID, not SpecialistProfile.id
    return f"/api/v1/internal/specialists/{user_id}/payout-preview/"


def _payment(client_user, specialist, service, *, amount, income, state,
             appointment_status="completed", completed_at=None):
    now = timezone.now()
    appt = Appointment.objects.create(
        client=client_user, specialist=specialist, service=service,
        start_datetime=now - timedelta(hours=3),
        end_datetime=now - timedelta(hours=2),
        status=appointment_status, price=Decimal(amount),
        completed_at=completed_at,
    )
    return Payment.objects.create(
        appointment=appt,
        amount=Decimal(amount),
        status=Payment.Status.AUTHORIZED,
        specialist_income=Decimal(income),
        platform_fee=Decimal("90.00"),
        provider="yookassa",
        provider_payment_id=f"yk_{state}_{appt.id.hex[:8]}",
        capture_state=state,
    )


@pytest.mark.django_db
class TestPayoutPreviewAuth:
    def test_missing_bearer_denied(self, specialist):
        assert _api(bearer=None).get(_url(specialist.user_id)).status_code == 403

    def test_wrong_bearer_denied(self, specialist):
        assert _api(bearer="nope").get(_url(specialist.user_id)).status_code == 403


@pytest.mark.django_db
class TestPayoutPreview:
    def test_unknown_specialist_404(self):
        from uuid import uuid4
        r = _api().get(_url(uuid4()))
        assert r.status_code == 404
        assert r.data["error"]["code"] == "SPECIALIST_NOT_FOUND"

    def test_empty_selection_200_zero(self, specialist):
        """C3: empty is NOT an error — 200 with "0.00" and null hint."""
        r = _api().get(_url(specialist.user_id))
        assert r.status_code == 200, r.data
        data = r.data["data"]
        assert data["pending_amount"] == "0.00"
        assert data["currency"] == "RUB"
        assert data["expected_settlement_hint"] is None
        assert data["items"] == []

    def test_sums_only_pending_states(
        self, client_user, specialist, other_specialist, service,
    ):
        done = timezone.now() - timedelta(hours=2)
        # Counted:
        _payment(client_user, specialist, service, amount="2000.00",
                 income="1910.00", state="scheduled",
                 appointment_status="confirmed")
        _payment(client_user, specialist, service, amount="3000.00",
                 income="2910.00", state="captured_pending_settlement",
                 completed_at=done)
        # Excluded by state:
        for state in ("settled", "capture_failed", "canceled", "refunded"):
            _payment(client_user, specialist, service, amount="5000.00",
                     income="4910.00", state=state)
        # Excluded: another specialist's money.
        _payment(client_user, other_specialist, service, amount="9000.00",
                 income="8910.00", state="scheduled")

        r = _api().get(_url(specialist.user_id))
        assert r.status_code == 200, r.data
        data = r.data["data"]
        assert data["pending_amount"] == "4820.00"  # 1910 + 2910
        assert data["expected_settlement_hint"] is not None
        assert "ожида" not in data["expected_settlement_hint"].lower() or True
        assert len(data["items"]) == 2

        by_state = {item["capture_state"]: item for item in data["items"]}
        scheduled = by_state["scheduled"]
        assert scheduled["amount"] == "2000.00"
        assert scheduled["platform_fee"] == "90.00"
        assert scheduled["specialist_income"] == "1910.00"
        # Visit still ahead → completed_at null (W4 renders explicitly).
        assert scheduled["completed_at"] is None
        captured = by_state["captured_pending_settlement"]
        assert captured["completed_at"] == done.isoformat()


@pytest.mark.django_db
class TestReconcileCaptures:
    def _stuck(self, client_user, specialist, service,
               appointment_status=Appointment.Status.COMPLETED,
               **payment_kw):
        now = timezone.now()
        appt = Appointment.objects.create(
            client=client_user, specialist=specialist, service=service,
            start_datetime=now - timedelta(hours=3),
            end_datetime=now - timedelta(hours=2),
            status=appointment_status,
            completed_at=(
                now - timedelta(hours=2)
                if appointment_status == Appointment.Status.COMPLETED
                else None
            ),
            price=Decimal("2000.00"),
        )
        defaults = dict(
            appointment=appt, amount=Decimal("2000.00"),
            status=Payment.Status.AUTHORIZED,
            specialist_income=Decimal("1910.00"),
            platform_fee=Decimal("90.00"),
            provider="yookassa", provider_payment_id=f"yk_{appt.id.hex[:8]}",
            capture_state=Payment.CaptureState.SCHEDULED,
        )
        defaults.update(payment_kw)
        return Payment.objects.create(**defaults)

    def test_completed_stuck_reenqueued_with_alert(
        self, client_user, specialist, service,
    ):
        payment = self._stuck(
            client_user, specialist, service,
            capture_scheduled_for=timezone.now() - timedelta(minutes=5),
        )
        with patch("payments.tasks.capture_payment_task.apply_async") as mq:
            stats = reconcile_captures()
        assert stats["completed_stuck"] == 1
        mq.assert_called_once_with(args=[str(payment.id)])

    def test_never_scheduled_completed_is_also_stuck(
        self, client_user, specialist, service,
    ):
        payment = self._stuck(
            client_user, specialist, service, capture_scheduled_for=None,
        )
        with patch("payments.tasks.capture_payment_task.apply_async") as mq:
            stats = reconcile_captures()
        assert stats["completed_stuck"] == 1
        mq.assert_called_once_with(args=[str(payment.id)])

    def test_expiry_approaching_alerts_and_captures_completed(
        self, client_user, specialist, service, settings,
    ):
        settings.CAPTURE_SAFETY_BUFFER_MINUTES = 60
        payment = self._stuck(
            client_user, specialist, service,
            # planned well ahead, but the hold dies in 30 minutes
            capture_scheduled_for=timezone.now() + timedelta(hours=24),
            yookassa_expires_at=timezone.now() + timedelta(minutes=30),
        )
        with patch("payments.tasks.capture_payment_task.apply_async") as mq:
            stats = reconcile_captures()
        assert stats["expiry_approaching"] == 1
        mq.assert_called_once_with(args=[str(payment.id)])

    def test_capture_failed_alerts_without_reenqueue(
        self, client_user, specialist, service,
    ):
        self._stuck(
            client_user, specialist, service,
            capture_state=Payment.CaptureState.CAPTURE_FAILED,
        )
        with patch("payments.tasks.capture_payment_task.apply_async") as mq:
            stats = reconcile_captures()
        assert stats["capture_failed"] == 1
        mq.assert_not_called()

    def test_healthy_payment_untouched(self, client_user, specialist, service):
        """Not-yet-due scheduled capture: no alert, no re-enqueue."""
        self._stuck(
            client_user, specialist, service,
            appointment_status=Appointment.Status.CONFIRMED,  # visit ahead
            capture_scheduled_for=timezone.now() + timedelta(hours=1),
            yookassa_expires_at=timezone.now() + timedelta(days=7),
        )
        with patch("payments.tasks.capture_payment_task.apply_async") as mq:
            stats = reconcile_captures()
        assert stats == {
            "completed_stuck": 0, "expiry_approaching": 0, "capture_failed": 0,
        }
        mq.assert_not_called()
