"""ActionService — handles POST /api/v1/ai/chat/{conversation_id}/action/.

The /chat endpoint produces a "card" (action_data) describing what the
LLM proposes — show_specialists, show_slots, confirm_booking,
ask_clarification. The user confirms or rejects via this endpoint, and
we perform any side effect (e.g. actually creating an Appointment for
confirm_booking).

Booking creation goes through CreateBookingService with idempotency_key
`ai-{conversation_id}-{datetime}` so a duplicate POST returns the same
appointment instead of failing or creating two.

NOTE: source=ai tracking is currently encoded in the idempotency_key
prefix only — Appointment.source field is a follow-up (see
docs/AI_CHAT_PLAN.md Open follow-ups). To find AI-created bookings:
``Appointment.objects.filter(idempotency_key__startswith="ai-")``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any
from uuid import UUID

from django.utils import timezone

from appointments.application.dto import CreateBookingDTO
from appointments.application.services.create_booking_service import (
    CreateBookingService,
)
from appointments.domain.exceptions import (
    BookingDomainError,
    SlotNotAvailableError,
)

from ai.exceptions import AIInvalidAction, AINotOwner
from ai.models import Conversation, Message
from ai.tools import ActionType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionResultDTO:
    success: bool
    appointment_id: UUID | None
    appointment_payload: dict[str, Any] | None
    next_message: Message | None
    next_action_type: str
    next_action_data: dict[str, Any] | None
    error_code: str | None = None
    error_details: dict[str, Any] | None = None


class ActionService:
    """Confirms / rejects an AI-proposed action and executes its side effect."""

    def __init__(
        self, *, booking_service: CreateBookingService | None = None
    ) -> None:
        self._booking_service = booking_service or CreateBookingService()

    def execute(
        self,
        *,
        actor,
        conversation: Conversation,
        action_type: str,
        confirmed: bool,
        data: dict[str, Any] | None,
        request_tenant_id: UUID | None = None,
    ) -> ActionResultDTO:
        if conversation.user_id != actor.id:
            raise AINotOwner("conversation belongs to another user")

        data = data or {}

        if action_type == ActionType.CONFIRM_BOOKING:
            return self._handle_confirm_booking(
                conversation=conversation,
                actor=actor,
                confirmed=confirmed,
                data=data,
                request_tenant_id=request_tenant_id,
            )
        if action_type == ActionType.SHOW_SPECIALISTS:
            return self._handle_specialist_selected(
                conversation=conversation, data=data
            )
        if action_type == ActionType.ASK_CLARIFICATION:
            return self._handle_clarification_answer(
                conversation=conversation, data=data
            )
        if action_type == ActionType.SHOW_SLOTS:
            return self._handle_slot_selected(
                conversation=conversation, data=data
            )

        raise AIInvalidAction(f"unsupported action_type: {action_type}")

    # ------------------------------------------------------------------
    # confirm_booking
    # ------------------------------------------------------------------
    def _handle_confirm_booking(
        self,
        *,
        conversation: Conversation,
        actor,
        confirmed: bool,
        data: dict[str, Any],
        request_tenant_id: UUID | None,
    ) -> ActionResultDTO:
        if not confirmed:
            self._record_user_message(
                conversation, "Не подтверждаю запись"
            )
            return ActionResultDTO(
                success=True,
                appointment_id=None,
                appointment_payload=None,
                next_message=None,
                next_action_type="",
                next_action_data=None,
            )

        try:
            specialist_id = UUID(str(data["specialist_id"]))
            service_id = UUID(str(data["service_id"]))
            raw_dt = str(data["datetime"])
        except (KeyError, ValueError, TypeError) as exc:
            raise AIInvalidAction(
                f"confirm_booking missing required fields: {exc}"
            ) from exc

        try:
            slot_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AIInvalidAction(f"invalid datetime: {raw_dt}") from exc

        if slot_dt.tzinfo is None:
            slot_dt = slot_dt.replace(tzinfo=dt_timezone.utc)

        idempotency_key = self._idempotency_key(conversation.id, slot_dt)
        self._record_user_message(conversation, "Подтверждаю запись")

        # #716: tenant context comes from the request (X-Tenant header
        # via TenantContextMiddleware → request.tenant), mirroring the
        # HTTP booking path in appointments/views.py:163. Earlier we
        # used actor.tenant_id as a proxy — that's the legacy primary
        # FK and disagrees with the active request tenant whenever the
        # customer browses a different provider (the whole point of the
        # multi-provider model post-#246 sub-phase 1.E, where customer
        # JWT carries no primary). Passing the request tenant lets
        # Variant E grant TUR against the tenant the user is actually
        # acting in, not their historical default.
        dto = CreateBookingDTO(
            client_id=actor.id,
            specialist_id=specialist_id,
            service_id=service_id,
            start_at=slot_dt,
            idempotency_key=idempotency_key,
            request_tenant_id=request_tenant_id,
        )

        try:
            result = self._booking_service.execute(dto)
        except SlotNotAvailableError as exc:
            logger.info(
                "ai.action.confirm_booking slot_taken conv=%s",
                conversation.id,
            )
            return ActionResultDTO(
                success=False,
                appointment_id=None,
                appointment_payload=None,
                next_message=None,
                next_action_type="",
                next_action_data=None,
                error_code="SLOT_NOT_AVAILABLE",
                error_details={"reason": str(exc)},
            )
        except BookingDomainError as exc:
            logger.warning(
                "ai.action.confirm_booking domain_error conv=%s err=%s",
                conversation.id, exc,
            )
            return ActionResultDTO(
                success=False,
                appointment_id=None,
                appointment_payload=None,
                next_message=None,
                next_action_type="",
                next_action_data=None,
                error_code="BOOKING_FAILED",
                error_details={"reason": str(exc)},
            )

        # Save a follow-up assistant message confirming the booking. We
        # don't re-call the LLM here in MVP — a deterministic message is
        # cheaper, faster, and impossible to hallucinate.
        follow_up = Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            content=self._format_booking_confirmation(result),
            action_type="",
            action_data=None,
        )
        conversation.last_message_at = timezone.now()
        conversation.save(update_fields=["last_message_at"])

        appointment_payload = {
            "id": str(result.booking_id),
            "status": result.status,
            "start_at": result.start_at.isoformat(),
            "end_at": result.end_at.isoformat(),
            "service_name": result.service_name,
            "duration_minutes": result.duration_minutes,
            "price": result.price,
        }

        return ActionResultDTO(
            success=True,
            appointment_id=result.booking_id,
            appointment_payload=appointment_payload,
            next_message=follow_up,
            next_action_type="",
            next_action_data=None,
        )

    # ------------------------------------------------------------------
    # show_specialists / show_slots / ask_clarification — record and pass
    # ------------------------------------------------------------------
    def _handle_specialist_selected(
        self, *, conversation: Conversation, data: dict[str, Any]
    ) -> ActionResultDTO:
        selected = data.get("selected_specialist_id")
        text = (
            f"Выбираю мастера {selected}" if selected else "Выбор не сделан"
        )
        msg = self._record_user_message(conversation, text)
        return ActionResultDTO(
            success=True,
            appointment_id=None,
            appointment_payload=None,
            next_message=msg,
            next_action_type="",
            next_action_data=None,
        )

    def _handle_slot_selected(
        self, *, conversation: Conversation, data: dict[str, Any]
    ) -> ActionResultDTO:
        chosen_dt = data.get("datetime")
        text = (
            f"Выбираю слот {chosen_dt}" if chosen_dt else "Слот не выбран"
        )
        msg = self._record_user_message(conversation, text)
        return ActionResultDTO(
            success=True,
            appointment_id=None,
            appointment_payload=None,
            next_message=msg,
            next_action_type="",
            next_action_data=None,
        )

    def _handle_clarification_answer(
        self, *, conversation: Conversation, data: dict[str, Any]
    ) -> ActionResultDTO:
        answer = data.get("answer") or ""
        if not answer:
            raise AIInvalidAction("ask_clarification requires data.answer")
        msg = self._record_user_message(conversation, answer)
        return ActionResultDTO(
            success=True,
            appointment_id=None,
            appointment_payload=None,
            next_message=msg,
            next_action_type="",
            next_action_data=None,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _idempotency_key(conversation_id: UUID, slot_dt: datetime) -> str:
        # Prefix `ai-` lets us audit AI-created appointments without a
        # schema change. Truncated to <100 chars (Appointment field cap).
        return f"ai-{conversation_id}-{slot_dt.isoformat()}"[:100]

    @staticmethod
    def _record_user_message(
        conversation: Conversation, text: str
    ) -> Message:
        msg = Message.objects.create(
            conversation=conversation,
            role=Message.Role.USER,
            content=text,
        )
        conversation.last_message_at = timezone.now()
        conversation.save(update_fields=["last_message_at"])
        return msg

    @staticmethod
    def _format_booking_confirmation(result) -> str:
        # Result is a BookingResultDTO from create_booking_service.
        local = result.start_at.astimezone()
        return (
            f"Записала вас на {local.strftime('%d.%m %H:%M')} "
            f"({result.service_name}). Напомню за час."
        )
