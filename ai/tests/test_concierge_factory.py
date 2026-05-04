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
    def test_returns_core_context_for_user_with_no_specialists(self):
        user = make_user(city="Penza")
        core = build_specialist_context_for_actor(user)
        assert isinstance(core, SpecialistContext)
        # No active specialists in DB → empty candidates, but the type
        # contract is preserved (AIConcierge would still accept it).
        assert core.candidates == []


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
    def test_returns_aiconcierge_with_uuid_tools(self, mock_client):
        mock_client.return_value = MagicMock()
        user = make_user()
        c = get_concierge_for(user)
        assert isinstance(c, AIConcierge)
        # Tool wire-format must be UUID strings (not int) for Ayla.
        defs = c._tool_definitions  # noqa: SLF001 — tested implementation seam
        master_id_schema = defs[0]["function"]["parameters"]["properties"]["master_ids"]
        assert master_id_schema["items"]["type"] == "string"

    @patch("ai.concierge_factory.get_async_openai_client")
    def test_action_type_constants_match_wire_format(self, mock_client):
        # Sanity: ayla-ai-core's ActionType set is what Slice B will
        # surface to mobile. Document the breaking change up-front.
        assert ActionType.SHOW_MASTERS == "show_masters"
        assert ActionType.SHOW_MY_BOOKINGS == "show_my_bookings"
        # Local ai/tools.py still uses "show_specialists" / "show_appointments";
        # Slice B aligns them by replacing local tools.py with shared imports.
