"""Handler wiring tests — billing.handlers.on_booking_completed (R-5).

W1 registers this handler in EVENT_HANDLERS; here it is invoked
directly with a duck-typed event (same .data/.id surface as
OutboxEvent).
"""
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from billing.handlers import on_booking_completed
from billing.models import BookingFee


def _event(data, event_id=None):
    return SimpleNamespace(id=event_id or uuid4(), data=data)


class TestOnBookingCompleted:
    def test_accrues_fee(self, db, appointment, subscription):
        with patch("billing.events.emit_fee_charged"):
            on_booking_completed(_event({"appointment_id": str(appointment.id)}))
        assert BookingFee.objects.filter(appointment=appointment).count() == 1

    def test_missing_appointment_id_is_noop(self, db):
        on_booking_completed(_event({}))
        assert BookingFee.objects.count() == 0

    def test_unknown_appointment_is_noop(self, db):
        on_booking_completed(_event({"appointment_id": str(uuid4())}))
        assert BookingFee.objects.count() == 0

    def test_accrual_error_does_not_propagate(self, db, appointment):
        with patch(
            "billing.handlers.accrue_booking_fee",
            side_effect=RuntimeError("boom"),
        ), patch("billing.handlers.sentry_sdk.capture_exception") as sentry_exc:
            on_booking_completed(_event({"appointment_id": str(appointment.id)}))
        sentry_exc.assert_called_once()
