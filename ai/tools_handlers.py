"""Tool-call handlers — validate LLM args + shape action_data per spec.

These run during `chat_service.send_message()` AFTER the LLM emits a
tool_call. They are SIDE-EFFECT-FREE — they only validate IDs and load
display data. The actual side effect (creating an appointment) happens
later in `action_service.execute()` after the user explicitly confirms.

Each handler returns the `action.data` payload shape defined in API
Spec v2.0 §AI ASSISTANT (ShowSpecialistsData / ShowSlotsData / ...).

If the LLM hallucinates an invalid ID we return a "fallback" action
of type `ask_clarification` so the user is never shown a broken card —
this is preferred to raising and aborting the whole chat turn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from appointments.application.dto import GetAvailabilityDTO
from appointments.application.services.availability_query_service import (
    AvailabilityQueryService,
)
from appointments.models import Appointment
from services.models import Service
from users.models import SpecialistProfile

from ai.application.services.specialist_context_builder import (
    SpecialistContext,
)
from ai.tools import ActionType

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolResult:
    """What the handler hands back to chat_service for the API response."""

    action_type: str
    action_data: dict[str, Any]


def _safe_uuid(value: Any) -> UUID | None:
    if not value:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _fallback_clarification(reason: str) -> ToolResult:
    """LLM gave us bad args — bounce to a clarification question."""
    logger.warning("ai.tool_call.fallback reason=%s", reason)
    return ToolResult(
        action_type=ActionType.ASK_CLARIFICATION,
        action_data={
            "question": "Уточните, пожалуйста, что вы хотели бы найти?",
            "options": [],
        },
    )


# ---------------------------------------------------------------------------
# show_specialists
# ---------------------------------------------------------------------------

def handle_show_specialists(
    args: dict[str, Any], context: SpecialistContext
) -> ToolResult:
    """Validate that all specialist_ids are in the candidate set, then shape.

    Drops invalid IDs silently (does not error) — partial result is
    better than dead chat turn.
    """
    raw_ids = args.get("specialist_ids") or []
    scores = args.get("match_scores") or []
    reasons = args.get("match_reasons") or []
    explanation = args.get("explanation") or ""

    valid_ids = [_safe_uuid(rid) for rid in raw_ids]
    valid_ids = [vid for vid in valid_ids if vid is not None]
    valid_set = {vid for vid in valid_ids if vid in context.candidate_ids}

    if not valid_set:
        return _fallback_clarification("show_specialists_no_valid_ids")

    by_id = {c.id: c for c in context.candidates}
    items: list[dict[str, Any]] = []
    for idx, sid in enumerate(valid_ids):
        if sid not in valid_set:
            continue
        c = by_id[sid]
        items.append({
            "specialist": {
                "id": str(c.id),
                "name": c.display_name,
                "rating": float(c.rating),
                "reviews_count": c.reviews_count,
                "address": c.address,
                "distance_km": c.distance_km,
                "services_preview": c.services_preview,
            },
            "match_score": scores[idx] if idx < len(scores) else None,
            "match_reasons": reasons[idx] if idx < len(reasons) else [],
        })

    return ToolResult(
        action_type=ActionType.SHOW_SPECIALISTS,
        action_data={
            "specialists": items,
            "explanation": explanation,
        },
    )


# ---------------------------------------------------------------------------
# show_slots
# ---------------------------------------------------------------------------

def handle_show_slots(
    args: dict[str, Any],
    *,
    availability_service: AvailabilityQueryService | None = None,
) -> ToolResult:
    specialist_id = _safe_uuid(args.get("specialist_id"))
    service_id = _safe_uuid(args.get("service_id"))
    date_str = args.get("date") or ""

    if not specialist_id or not service_id:
        return _fallback_clarification("show_slots_invalid_ids")
    try:
        target_date = date.fromisoformat(date_str)
    except (ValueError, TypeError):
        return _fallback_clarification("show_slots_invalid_date")

    # Resolve service for response shape (name + duration + price).
    service = (
        Service.objects.filter(
            id=service_id, specialist_id=specialist_id, is_active=True
        ).first()
    )
    if service is None:
        return _fallback_clarification("show_slots_service_not_found")

    svc = availability_service or AvailabilityQueryService()
    dto = GetAvailabilityDTO(
        specialist_id=specialist_id,
        target_date=target_date,
        service_id=service_id,
    )
    try:
        result = svc.get_day_availability(dto)
    except Exception as exc:  # noqa: BLE001 — best-effort fallback
        logger.exception("ai.show_slots availability_error: %s", exc)
        return _fallback_clarification("show_slots_availability_error")

    return ToolResult(
        action_type=ActionType.SHOW_SLOTS,
        action_data={
            "specialist_id": str(specialist_id),
            "service_id": str(service_id),
            "date": target_date.isoformat(),
            "service": {
                "id": str(service.id),
                "name": service.name,
                "duration_minutes": service.duration_minutes,
                "price": str(service.price),
            },
            "slots": [s.isoformat() for s in getattr(result, "slots", []) or []],
        },
    )


# ---------------------------------------------------------------------------
# confirm_booking
# ---------------------------------------------------------------------------

def handle_confirm_booking(args: dict[str, Any]) -> ToolResult:
    specialist_id = _safe_uuid(args.get("specialist_id"))
    service_id = _safe_uuid(args.get("service_id"))
    raw_dt = args.get("datetime") or ""

    if not specialist_id or not service_id:
        return _fallback_clarification("confirm_booking_invalid_ids")
    try:
        slot_dt = datetime.fromisoformat(raw_dt.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return _fallback_clarification("confirm_booking_invalid_datetime")

    specialist = SpecialistProfile.objects.filter(id=specialist_id).first()
    service = Service.objects.filter(
        id=service_id, specialist_id=specialist_id
    ).first()
    if specialist is None or service is None:
        return _fallback_clarification("confirm_booking_specialist_or_service_missing")

    return ToolResult(
        action_type=ActionType.CONFIRM_BOOKING,
        action_data={
            "specialist_id": str(specialist.id),
            "specialist_name": specialist.display_name,
            "service_id": str(service.id),
            "service_name": service.name,
            "datetime": slot_dt.isoformat(),
            "price": str(service.price),
            "address": specialist.address,
            "duration_minutes": service.duration_minutes,
        },
    )


# ---------------------------------------------------------------------------
# show_appointments
# ---------------------------------------------------------------------------

def handle_show_appointments(
    args: dict[str, Any], *, client_id: UUID
) -> ToolResult:
    filter_kind = args.get("filter") or "upcoming"
    qs = Appointment.objects.filter(client_id=client_id).select_related(
        "specialist", "service"
    )
    now = datetime.now()
    if filter_kind == "upcoming":
        qs = qs.filter(start_datetime__gte=now).exclude(
            status__in=Appointment.TERMINAL_STATUSES
        )
    elif filter_kind == "past":
        qs = qs.filter(start_datetime__lt=now)
    qs = qs.order_by("start_datetime")[:10]

    appts = []
    for a in qs:
        appts.append({
            "id": str(a.id),
            "start_datetime": a.start_datetime.isoformat(),
            "end_datetime": a.end_datetime.isoformat(),
            "status": a.status,
            "specialist": {
                "id": str(a.specialist_id),
                "name": a.specialist.display_name,
            },
            "service": {
                "id": str(a.service_id),
                "name": a.service.name,
                "price": str(a.snapshot_price or a.price),
            },
        })

    return ToolResult(
        action_type=ActionType.SHOW_APPOINTMENTS,
        action_data={"appointments": appts, "filter": filter_kind},
    )


# ---------------------------------------------------------------------------
# ask_clarification
# ---------------------------------------------------------------------------

def handle_ask_clarification(args: dict[str, Any]) -> ToolResult:
    question = args.get("question") or ""
    options = args.get("options") or []
    if not question:
        return _fallback_clarification("ask_clarification_no_question")
    return ToolResult(
        action_type=ActionType.ASK_CLARIFICATION,
        action_data={"question": question, "options": list(options)[:5]},
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

HANDLERS = {
    "show_specialists": "specialists",
    "show_slots": "slots",
    "confirm_booking": "confirm",
    "show_appointments": "appointments",
    "ask_clarification": "clarification",
}


def dispatch_tool_call(
    tool_name: str,
    args: dict[str, Any],
    *,
    context: SpecialistContext,
    client_id: UUID | None,
    availability_service: AvailabilityQueryService | None = None,
) -> ToolResult:
    """Route a single tool_call to the right handler.

    Unknown tools return a clarification fallback rather than raising.
    """
    if tool_name == "show_specialists":
        return handle_show_specialists(args, context)
    if tool_name == "show_slots":
        return handle_show_slots(args, availability_service=availability_service)
    if tool_name == "confirm_booking":
        return handle_confirm_booking(args)
    if tool_name == "show_appointments":
        if client_id is None:
            return _fallback_clarification("show_appointments_anonymous_blocked")
        return handle_show_appointments(args, client_id=client_id)
    if tool_name == "ask_clarification":
        return handle_ask_clarification(args)

    logger.warning("ai.tool_call.unknown name=%s", tool_name)
    return _fallback_clarification(f"unknown_tool:{tool_name}")
