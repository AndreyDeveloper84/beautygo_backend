"""Unit tests for ActionService."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock

import pytest

from ai.application.services.action_service import ActionService
from ai.exceptions import AIInvalidAction, AINotOwner
from ai.models import Conversation, Message
from ai.tests.factories import make_conversation, make_user
from ai.tools import ActionType
from appointments.application.dto import CreateBookingDTO
from appointments.domain.exceptions import SlotNotAvailableError
from appointments.application.dto import BookingResultDTO


pytestmark = pytest.mark.django_db


def _booking_result(booking_id, start_at):
    end = start_at + timedelta(hours=1)
    return BookingResultDTO(
        booking_id=booking_id,
        status="pending",
        start_at=start_at,
        end_at=end,
        service_name="Маникюр",
        duration_minutes=60,
        price="1500.00",
        payment_id=None,
        payment_client_secret=None,
    )


class TestOwnership:
    def test_other_users_conversation_raises(self, db, client_user):
        other = make_user(role="client")
        conv = make_conversation(user=other)
        with pytest.raises(AINotOwner):
            ActionService().execute(
                actor=client_user,
                conversation=conv,
                action_type=ActionType.CONFIRM_BOOKING,
                confirmed=False,
                data={},
            )


class TestConfirmBooking:
    def test_confirmed_calls_booking_service_with_idempotency_prefix(
        self, client_user
    ):
        import uuid as _uuid
        booking_svc = MagicMock()
        slot_dt = datetime(2026, 6, 1, 14, 0, tzinfo=dt_timezone.utc)
        booking_id = _uuid.uuid4()
        booking_svc.execute.return_value = _booking_result(booking_id, slot_dt)

        conv = make_conversation(user=client_user)
        spec_id = _uuid.uuid4()
        svc_id = _uuid.uuid4()

        result = ActionService(booking_service=booking_svc).execute(
            actor=client_user,
            conversation=conv,
            action_type=ActionType.CONFIRM_BOOKING,
            confirmed=True,
            data={
                "specialist_id": str(spec_id),
                "service_id": str(svc_id),
                "datetime": slot_dt.isoformat(),
            },
        )

        booking_svc.execute.assert_called_once()
        dto = booking_svc.execute.call_args.args[0]
        assert isinstance(dto, CreateBookingDTO)
        assert dto.idempotency_key.startswith(f"ai-{conv.id}-")
        assert result.success is True
        assert result.appointment_id == booking_id

    def test_not_confirmed_records_decline(self, client_user):
        booking_svc = MagicMock()
        conv = make_conversation(user=client_user)
        result = ActionService(booking_service=booking_svc).execute(
            actor=client_user,
            conversation=conv,
            action_type=ActionType.CONFIRM_BOOKING,
            confirmed=False,
            data={},
        )
        booking_svc.execute.assert_not_called()
        assert result.success is True
        assert result.appointment_id is None
        # User decline message recorded.
        assert Message.objects.filter(
            conversation=conv, role=Message.Role.USER
        ).count() == 1

    def test_slot_taken_returns_error_code(self, client_user):
        import uuid as _uuid
        booking_svc = MagicMock()
        booking_svc.execute.side_effect = SlotNotAvailableError("taken")
        conv = make_conversation(user=client_user)
        slot_dt = datetime(2026, 6, 1, 14, 0, tzinfo=dt_timezone.utc)

        result = ActionService(booking_service=booking_svc).execute(
            actor=client_user,
            conversation=conv,
            action_type=ActionType.CONFIRM_BOOKING,
            confirmed=True,
            data={
                "specialist_id": str(_uuid.uuid4()),
                "service_id": str(_uuid.uuid4()),
                "datetime": slot_dt.isoformat(),
            },
        )
        assert result.success is False
        assert result.error_code == "SLOT_NOT_AVAILABLE"

    def test_missing_datetime_raises_invalid_action(self, client_user):
        booking_svc = MagicMock()
        conv = make_conversation(user=client_user)
        with pytest.raises(AIInvalidAction):
            ActionService(booking_service=booking_svc).execute(
                actor=client_user,
                conversation=conv,
                action_type=ActionType.CONFIRM_BOOKING,
                confirmed=True,
                data={"specialist_id": "x", "service_id": "y"},
            )


class TestOtherActions:
    def test_show_specialists_selected_records_user_message(self, client_user):
        conv = make_conversation(user=client_user)
        result = ActionService().execute(
            actor=client_user,
            conversation=conv,
            action_type=ActionType.SHOW_SPECIALISTS,
            confirmed=True,
            data={"selected_specialist_id": "anna"},
        )
        assert result.success is True
        assert Message.objects.filter(conversation=conv).count() == 1

    def test_ask_clarification_requires_answer(self, client_user):
        conv = make_conversation(user=client_user)
        with pytest.raises(AIInvalidAction):
            ActionService().execute(
                actor=client_user,
                conversation=conv,
                action_type=ActionType.ASK_CLARIFICATION,
                confirmed=True,
                data={},
            )

    def test_unknown_action_raises_invalid(self, client_user):
        conv = make_conversation(user=client_user)
        with pytest.raises(AIInvalidAction):
            ActionService().execute(
                actor=client_user,
                conversation=conv,
                action_type="unknown_action",
                confirmed=True,
                data={},
            )
