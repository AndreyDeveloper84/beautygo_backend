"""PII redaction for prompts before they cross to OpenAI.

api.openai.com sits in US jurisdiction; sending PII of RU citizens there
is a 152-ФЗ violation regardless of the proxy hop. This module strips the
high-confidence PII (phone, email) from any text we hand to the LLM.

Known gaps for the MVP (deliberately deferred):
- Last names (regex unreliable in Russian, would need NER)
- Free-form addresses
- Card numbers — out of scope for chat flows; payments never go through LLM
"""
from __future__ import annotations

import re

# RU phones: +7XXXXXXXXXX, 7XXXXXXXXXX, 8XXXXXXXXXX, with optional spaces,
# brackets, dashes between groups. Anchored on a non-digit boundary so we
# don't bite into longer ID numbers (e.g. order #87999000111 — 11 digits
# but not a phone).
_PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:\+7|7|8)"
    r"[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}"
    r"(?!\d)"
)

# Email — standard, conservative. Boundary on word characters so we don't
# clip URLs or markdown links.
_EMAIL_RE = re.compile(
    r"\b[\w.%+\-]+@[\w.\-]+\.[A-Za-z]{2,}\b"
)

PHONE_PLACEHOLDER = "[PHONE]"
EMAIL_PLACEHOLDER = "[EMAIL]"


def redact_pii(text: str) -> str:
    """Replace RU phones and email addresses with placeholders.

    Idempotent — running twice is a no-op (placeholders contain no PII
    patterns themselves). Caller doesn't need to know what was replaced;
    if a downstream flow needs to fill the value back, it should keep its
    own mapping rather than relying on this layer.
    """
    if not text:
        return text
    text = _PHONE_RE.sub(PHONE_PLACEHOLDER, text)
    text = _EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    return text


def has_pii(text: str) -> bool:
    """Cheap pre-check used by tests / metrics to flag prompts that would
    have leaked PII without redaction."""
    if not text:
        return False
    return bool(_PHONE_RE.search(text) or _EMAIL_RE.search(text))
