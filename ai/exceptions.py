"""AI-domain exceptions. View layer maps each to HTTP error codes per
API Spec v2.0 + 429 details.reason convention from docs/AI_CHAT_PLAN.md.
"""
from __future__ import annotations


class AIError(Exception):
    """Base for all AI-app exceptions."""


class AIUnavailable(AIError):
    """OpenAI key missing OR vendor call failed → 503 AI_UNAVAILABLE."""


class AIRateLimitExceeded(AIError):
    """Base for 429s. Subclasses set the `reason` for details mapping."""

    reason = "minute_throttle"


class AIDailyLimitExceeded(AIRateLimitExceeded):
    reason = "daily_token_limit"


class AIAnonymousLimitExceeded(AIRateLimitExceeded):
    reason = "anon_message_limit"


class AIInvalidAction(AIError):
    """Bad action_type or args → 400 INVALID_ACTION_TYPE."""


class AIConversationNotFound(AIError):
    """Conversation id unknown OR not owned → 404 CONVERSATION_NOT_FOUND."""


class AINotOwner(AIError):
    """Conversation belongs to another user → 403 NOT_OWNER."""
