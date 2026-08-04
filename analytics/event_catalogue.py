"""Whitelisted event names — single source of truth in code.

Why a code-side whitelist (not a DB lookup table):

- Adding an event = one-line code change + push, no migration.
- Mobile and backend ship the same string at the same time —
  drift is caught by BI dashboards on day one rather than silent
  type mismatches in the lookup table.
- The cardinality is small (dozens of events per Phase, not
  thousands). When that stops being true, promote to a model.

When mobile invents an event name not in here, we 400 with
``UNKNOWN_EVENT_NAME`` so the API contract stays explicit. To add a
new event:

1. Add the constant + entry in ``EVENT_NAMES`` here.
2. Document the expected ``payload`` shape in the Notion catalogue.
3. Mobile starts emitting it.

Out of scope here: payload schema validation. Per-event payload
shapes drift over time and the cost of a JSON-schema check on every
ingest is not worth the friction. BI queries do schema-on-read.
"""
from __future__ import annotations


# ── Phase 0 — Booking flow ──
BOOKING_VIEWED = "booking_viewed"
BOOKING_CREATED = "booking_created"
BOOKING_CANCELLED = "booking_cancelled"
BOOKING_RESCHEDULED = "booking_rescheduled"
BOOKING_COMPLETED = "booking_completed"

# ── Phase 1 — AI Chat ──
AI_CHAT_OPENED = "ai_chat_opened"
AI_CHAT_MESSAGE_SENT = "ai_chat_message_sent"
AI_ACTION_SHOWN = "ai_action_shown"          # show_specialists / show_slots
AI_ACTION_CONFIRMED = "ai_action_confirmed"  # confirm_booking accepted
AI_ACTION_REJECTED = "ai_action_rejected"
AI_CLARIFICATION_ANSWERED = "ai_clarification_answered"

# ── Phase 1 — Food Scanner / Nutrition ──
FOOD_SCAN_TAKEN = "food_scan_taken"
FOOD_SCAN_CONFIRMED = "food_scan_confirmed"
FOOD_LOG_ADDED_MANUAL = "food_log_added_manual"
WATER_LOGGED = "water_logged"
DAILY_SUMMARY_VIEWED = "daily_summary_viewed"

# ── Phase 1 — Personal Context (DRF-174 / DRF-230) ──
PERSONAL_CONTEXT_FIELD_SET = "personal_context_field_set"
PERSONAL_CONTEXT_FIELD_CLEARED = "personal_context_field_cleared"
CONTEXT_QUESTION_SKIPPED = "context_question_skipped"

# DRF-230 PR 3 — PersonalizationEngine event surface. Lets BI track
# how often we ask, how often the user answers, and how often the
# context actually steered a recommendation (the proof the engine
# pays its UX cost).
PROFILE_QUESTION_SHOWN = "profile_question_shown"
PROFILE_QUESTION_ANSWERED = "profile_question_answered"
PROFILE_QUESTION_SKIPPED = "profile_question_skipped"
CONTEXT_USED_IN_RECOMMENDATION = "context_used_in_recommendation"

# ── App lifecycle ──
APP_OPENED = "app_opened"
ONBOARDING_STARTED = "onboarding_started"
ONBOARDING_COMPLETED = "onboarding_completed"
PUSH_RECEIVED = "push_received"
PUSH_TAPPED = "push_tapped"

# ── Pro app — specialist surface ──
PRO_DASHBOARD_VIEWED = "pro_dashboard_viewed"
PRO_BOOKING_VIEWED = "pro_booking_viewed"
PRO_BOOKING_ACTIONED = "pro_booking_actioned"  # confirm / cancel / no-show

# ── Search / discovery ──
SEARCH_PERFORMED = "search_performed"
SPECIALIST_VIEWED = "specialist_viewed"
SPECIALIST_FAVORITED = "specialist_favorited"
SPECIALIST_UNFAVORITED = "specialist_unfavorited"

# ── 152-ФЗ / C5.2 (PILOT_CONTRACTS AMD-010) ──
# Audit trail for internal personal-data deletion. Server-side emit
# only (users/personal_context_events.emit_personal_data_deleted);
# payload carries actor/scope, NEVER the deleted personal values.
PERSONAL_DATA_DELETED = "personal_data_deleted"

# ── Identity binding audit (E2E-BOT-02B hardening) ──
# Durable audit trail for the internal proxy→real identity binding.
# Server-side emit only (users/identity_events.emit_identity_binding);
# payload carries operation/result/reason + id references, NEVER
# bearer tokens or other secrets.
EXTERNAL_IDENTITY_BOUND = "external_identity_bound"


EVENT_NAMES: frozenset[str] = frozenset({
    BOOKING_VIEWED, BOOKING_CREATED, BOOKING_CANCELLED,
    BOOKING_RESCHEDULED, BOOKING_COMPLETED,
    AI_CHAT_OPENED, AI_CHAT_MESSAGE_SENT,
    AI_ACTION_SHOWN, AI_ACTION_CONFIRMED, AI_ACTION_REJECTED,
    AI_CLARIFICATION_ANSWERED,
    FOOD_SCAN_TAKEN, FOOD_SCAN_CONFIRMED, FOOD_LOG_ADDED_MANUAL,
    WATER_LOGGED, DAILY_SUMMARY_VIEWED,
    PERSONAL_CONTEXT_FIELD_SET, PERSONAL_CONTEXT_FIELD_CLEARED,
    CONTEXT_QUESTION_SKIPPED,
    PROFILE_QUESTION_SHOWN, PROFILE_QUESTION_ANSWERED,
    PROFILE_QUESTION_SKIPPED, CONTEXT_USED_IN_RECOMMENDATION,
    APP_OPENED, ONBOARDING_STARTED, ONBOARDING_COMPLETED,
    PUSH_RECEIVED, PUSH_TAPPED,
    PRO_DASHBOARD_VIEWED, PRO_BOOKING_VIEWED, PRO_BOOKING_ACTIONED,
    SEARCH_PERFORMED, SPECIALIST_VIEWED,
    SPECIALIST_FAVORITED, SPECIALIST_UNFAVORITED,
    PERSONAL_DATA_DELETED,
    EXTERNAL_IDENTITY_BOUND,
})
