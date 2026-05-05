"""Tests for ai.concierge_factory — DRF-241 Slice A.

The factory wires Ayla deps (Django store, recommendation-engine context
builder, async OpenAI client) into ayla-ai-core's AIConcierge. Slice A
asserts the wiring is correct without mocking OpenAI — Slice B will add
end-to-end pipeline tests.
"""
from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest
from ayla_ai_core import AIConcierge, SpecialistContext
from ayla_ai_core.tools import ActionType

from ai.concierge_factory import (
    build_specialist_context_for_actor,
    get_concierge_for,
    render_ayla_system_prompt,
    to_core_specialist_context,
)
from ai.application.services.specialist_context_builder import (
    SpecialistCandidate as LocalSpecialistCandidate,
    SpecialistContext as LocalSpecialistContext,
)
from ai.tests.factories import make_user
from decimal import Decimal
from uuid import uuid4


pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
# to_core_specialist_context
# ---------------------------------------------------------------------------


class TestToCoreContext:
    def test_translates_local_to_core_with_frozen_id_set(self):
        ids = [uuid4() for _ in range(3)]
        local = LocalSpecialistContext(candidates=[
            LocalSpecialistCandidate(
                id=i, display_name=f"M{n}", rating=Decimal("4.8"),
                reviews_count=10, address="addr", distance_km=1.0,
                services_preview=["a", "b"],
            )
            for n, i in enumerate(ids)
        ])
        core = to_core_specialist_context(local)
        assert isinstance(core, SpecialistContext)
        assert core.candidate_ids == frozenset(ids)
        # Anti-hallucination layer 2 (services) is no-op until Slice B
        # plumbs real service IDs through the recommendation engine.
        assert core.candidate_service_ids == frozenset()

    def test_empty_local_context_yields_empty_core_context(self):
        core = to_core_specialist_context(LocalSpecialistContext(candidates=[]))
        assert core.candidates == []
        assert core.candidate_ids == frozenset()


# ---------------------------------------------------------------------------
# build_specialist_context_for_actor
# ---------------------------------------------------------------------------


class TestBuildContextForActor:
    def test_returns_local_context_for_user_with_no_specialists(self):
        # Slice B: returns the *local* SpecialistContext (with score /
        # distance / match_reasons metadata) so the local dispatcher can
        # produce rich action_data. The factory derives the ayla-ai-core
        # core context from this object before handing it to AIConcierge
        # (see TestGetConcierge::test_concierge_uses_local_context_through_factory).
        user = make_user(city="Penza")
        local = build_specialist_context_for_actor(user)
        assert isinstance(local, LocalSpecialistContext)
        # No active specialists in DB → empty candidates list.
        assert local.candidates == []


# ---------------------------------------------------------------------------
# render_ayla_system_prompt
# ---------------------------------------------------------------------------


class TestRenderPrompt:
    def test_uses_ayla_marketplace_voice(self):
        core = to_core_specialist_context(LocalSpecialistContext(candidates=[]))
        prompt = render_ayla_system_prompt(
            core, today=date(2026, 5, 3), client_name="Анна", bookings_count=2,
        )
        # Voice asserts: assistant_name "Ayla" should appear, and the
        # business descriptor follows the marketplace template.
        assert "Ayla" in prompt
        assert "2026-05-03" in prompt

    def test_extra_hint_renders_advisory_block(self):
        core = to_core_specialist_context(LocalSpecialistContext(candidates=[]))
        prompt = render_ayla_system_prompt(
            core, today=date(2026, 5, 3), extra_hint="Дефицит белка 4 дня подряд",
        )
        assert "Дефицит белка" in prompt
        assert "ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ" in prompt

    def test_empty_extra_hint_skips_block(self):
        core = to_core_specialist_context(LocalSpecialistContext(candidates=[]))
        prompt = render_ayla_system_prompt(core, today=date(2026, 5, 3))
        assert "ДОПОЛНИТЕЛЬНЫЙ КОНТЕКСТ" not in prompt


# ---------------------------------------------------------------------------
# get_concierge_for
# ---------------------------------------------------------------------------


class TestGetConcierge:
    @patch("ai.concierge_factory.get_async_openai_client")
    def test_returns_aiconcierge_with_local_tool_definitions(self, mock_client):
        # Slice B (variant d): Ayla keeps its own tool_definitions per
        # API spec v2.0 (`show_specialists` / `specialist_ids`) and
        # injects the local dispatcher via ayla-ai-core 0.6.0's
        # tool_dispatcher hook. The bot keeps its own naming locally too;
        # shared package is infra-only.
        mock_client.return_value = MagicMock()
        user = make_user()
        c = get_concierge_for(user)
        assert isinstance(c, AIConcierge)
        defs = c._tool_definitions  # noqa: SLF001 — tested implementation seam
        first_tool_name = defs[0]["function"]["name"]
        assert first_tool_name == "show_specialists"
        # Wire-format is UUID string (not int) — JSON Schema items.type=string.
        spec_ids = defs[0]["function"]["parameters"]["properties"][
            "specialist_ids"
        ]
        assert spec_ids["items"]["type"] == "string"

    @patch("ai.concierge_factory.get_async_openai_client")
    def test_concierge_has_tool_dispatcher_injected(self, mock_client):
        # The factory injects Ayla's local dispatch_tool_call so the
        # default ayla-ai-core dispatcher (which knows show_masters, not
        # show_specialists) is bypassed.
        mock_client.return_value = MagicMock()
        user = make_user()
        c = get_concierge_for(user)
        assert c._tool_dispatcher is not None  # noqa: SLF001

    @patch("ai.concierge_factory.get_async_openai_client")
    def test_wire_format_diverges_from_shared_package_intentionally(
        self, mock_client,
    ):
        # Sanity: shared ayla-ai-core uses bot-Формула naming
        # (show_masters / master_id). Ayla diverges per API spec v2.0
        # and keeps the divergence behind the tool_dispatcher hook —
        # converting wire-format would otherwise need a Notion-spec
        # change + mobile coordination + LLM retraining of the bot's
        # ~30 days of `show_masters` traffic.
        assert ActionType.SHOW_MASTERS == "show_masters"
        # Ayla's local equivalent — verify divergence is by design.
        from ai.tools import ActionType as AylaActionType
        assert AylaActionType.SHOW_SPECIALISTS == "show_specialists"
