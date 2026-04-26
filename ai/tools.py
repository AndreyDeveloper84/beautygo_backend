"""OpenAI tool definitions for the AI chat.

These are the function-call schemas passed to chat.completions.create(tools=[...]).
The LLM emits tool_calls matching one of these shapes; tool_handlers.py
validates the args and turns them into action_data per API Spec v2.0
§AI ASSISTANT shapes (ShowSpecialistsData, ShowSlotsData, etc.).

5 tools in MVP. voice_response + collect_context deferred per M4 scope reduction.
"""
from __future__ import annotations


SHOW_SPECIALISTS = {
    "type": "function",
    "function": {
        "name": "show_specialists",
        "description": (
            "Show recommended specialists with match reasons. "
            "Use after the client expresses what they want and you have "
            "candidates in the prompt context. Do NOT invent specialist IDs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "specialist_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "UUIDs of specialists to show, ordered by relevance",
                    "maxItems": 5,
                },
                "match_scores": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0, "maximum": 100},
                    "description": "Match score 0-100 per specialist, same order as ids",
                },
                "match_reasons": {
                    "type": "array",
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": "Per-specialist short reasons (1-3 each)",
                },
                "explanation": {
                    "type": "string",
                    "description": "Overall explanation why these specialists were picked",
                },
            },
            "required": ["specialist_ids", "explanation"],
        },
    },
}


SHOW_SLOTS = {
    "type": "function",
    "function": {
        "name": "show_slots",
        "description": (
            "Show available time slots for a specialist + service on a given date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "specialist_id": {"type": "string"},
                "service_id": {"type": "string"},
                "date": {
                    "type": "string",
                    "format": "date",
                    "description": "Target date YYYY-MM-DD",
                },
            },
            "required": ["specialist_id", "service_id", "date"],
        },
    },
}


CONFIRM_BOOKING = {
    "type": "function",
    "function": {
        "name": "confirm_booking",
        "description": (
            "Show a booking confirmation card. Does NOT create the appointment — "
            "the client confirms via a separate /action/ call. Only emit this "
            "when the client has explicitly chosen specialist + service + time."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "specialist_id": {"type": "string"},
                "service_id": {"type": "string"},
                "datetime": {
                    "type": "string",
                    "format": "date-time",
                    "description": "Slot start datetime, ISO 8601 with timezone",
                },
            },
            "required": ["specialist_id", "service_id", "datetime"],
        },
    },
}


SHOW_APPOINTMENTS = {
    "type": "function",
    "function": {
        "name": "show_appointments",
        "description": (
            "Show the client's existing appointments. Use when they ask "
            "'when is my appointment' or 'show my bookings'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "filter": {
                    "type": "string",
                    "enum": ["upcoming", "past", "all"],
                    "description": "Which subset of appointments to show",
                },
            },
        },
    },
}


ASK_CLARIFICATION = {
    "type": "function",
    "function": {
        "name": "ask_clarification",
        "description": (
            "Ask the client a clarifying question with optional suggested answers. "
            "Use when the request is ambiguous (e.g. 'какой день недели вам удобнее?')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional pre-filled answer chips",
                    "maxItems": 5,
                },
            },
            "required": ["question"],
        },
    },
}


TOOL_DEFINITIONS = [
    SHOW_SPECIALISTS,
    SHOW_SLOTS,
    CONFIRM_BOOKING,
    SHOW_APPOINTMENTS,
    ASK_CLARIFICATION,
]


# Action types the API accepts. Kept as string constants (not enum) so
# the wire format stays stable and tools.py stays the single source of
# truth for naming.
class ActionType:
    SHOW_SPECIALISTS = "show_specialists"
    SHOW_SLOTS = "show_slots"
    CONFIRM_BOOKING = "confirm_booking"
    SHOW_APPOINTMENTS = "show_appointments"
    ASK_CLARIFICATION = "ask_clarification"

    # Deferred (forward-compat): accepted but not implemented in MVP.
    VOICE_RESPONSE = "voice_response"
    COLLECT_CONTEXT = "collect_context"

    ALL_MVP = {
        SHOW_SPECIALISTS,
        SHOW_SLOTS,
        CONFIRM_BOOKING,
        SHOW_APPOINTMENTS,
        ASK_CLARIFICATION,
    }
