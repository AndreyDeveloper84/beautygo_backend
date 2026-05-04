"""AIConcierge factory — wires ayla-ai-core orchestrator with Ayla deps.

Per docs/AI_CHAT_PLAN.md rev. 3 §Pipeline. The factory is the single seam
where Ayla-specific dependencies (Django ORM store, recommendation-engine
context builder, async OpenAI client, brand voice) get injected into the
generic AIConcierge from ayla-ai-core.

`AIConcierge` itself is stateless — one instance per request is fine and
keeps the closure-over-actor pattern simple (context_builder reads
actor's profile, location, history). If pooling becomes a perf concern
later, lift `openai_client` and `store` into module-level singletons —
those are pickle-clean and request-independent.
"""
from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from ayla_ai_core import (
    AIConcierge,
    AYLA_MARKETPLACE_VOICE,
    SpecialistCandidate,
    SpecialistContext,
    _safe_uuid,
    build_specialist_context_from_candidates,
    build_tool_definitions,
    render_system_prompt,
)

from ai.application.services.specialist_context_builder import (
    SpecialistContext as LocalSpecialistContext,
    SpecialistContextBuilder,
)
from ai.services.llm_client import get_async_openai_client
from ai.stores import DjangoConversationStore

if TYPE_CHECKING:  # pragma: no cover
    from users.models import User


__all__ = [
    "build_specialist_context_for_actor",
    "get_concierge_for",
    "render_ayla_system_prompt",
    "to_core_specialist_context",
]


def to_core_specialist_context(
    local: LocalSpecialistContext,
) -> SpecialistContext[UUID]:
    """Convert local recommendation-engine output → ayla-ai-core context.

    The local DTO carries scoring metadata (match_reasons, distance_km,
    score) that's useful for the system prompt but not for AIConcierge's
    anti-hallucination layer — that only needs `candidate_ids` +
    `candidate_service_ids` frozensets, which the factory below builds.

    services_preview on the local side is just names without IDs, so the
    service-level anti-hallucination on `confirm_booking` is no-op for
    Ayla today. Slice B will surface real service IDs through the
    recommendation engine so layer-2 validation kicks in.
    """
    candidates = [
        SpecialistCandidate(
            id=c.id,
            name=c.display_name,
            specialization="",
            services=[],
        )
        for c in local.candidates
    ]
    return build_specialist_context_from_candidates(candidates)


def build_specialist_context_for_actor(actor: "User") -> SpecialistContext[UUID]:
    """Build the per-request candidate set for the LLM context.

    Closure-friendly: AIConcierge expects `Callable[[], SpecialistContext]`,
    so the caller wraps this in a lambda that captures `actor`. Reads
    profile location + city like the local chat_service did so the
    recommendation engine's geo-filter still applies.
    """
    builder = SpecialistContextBuilder()
    profile = getattr(actor, "profile", None)
    city = getattr(profile, "city", None) if profile else None
    lat: float | None = None
    lon: float | None = None
    if profile is not None:
        lat = (
            float(profile.default_location_lat)
            if profile.default_location_lat is not None
            else None
        )
        lon = (
            float(profile.default_location_lng)
            if profile.default_location_lng is not None
            else None
        )
    client_id = actor.id if not getattr(actor, "is_guest", False) else None
    local = builder.build(
        client_id=client_id, client_lat=lat, client_lon=lon, city=city,
    )
    return to_core_specialist_context(local)


def render_ayla_system_prompt(
    specialist_context: SpecialistContext[UUID],
    *,
    today: date,
    client_name: str | None = None,
    bookings_count: int = 0,
    extra_hint: str = "",
) -> str:
    """Render the Ayla system prompt using ayla-ai-core's voice config.

    `extra_hint` is the DRF-248 cross-domain bridge slot — caller fetches
    weekly nutrition deficits from the bot and feeds the rendered hint
    here. Empty string = no signal, prompt skips the block entirely.
    """
    return render_system_prompt(
        today=today,
        client_name=client_name or "",
        bookings_count=bookings_count,
        specialist_context=specialist_context,
        voice_config=AYLA_MARKETPLACE_VOICE,
        extra_hint=extra_hint,
    )


def get_concierge_for(actor: "User") -> AIConcierge:
    """Build a per-request AIConcierge instance bound to this actor.

    Why per-request: `context_builder` closes over `actor` to apply geo +
    history filters. `AIConcierge` itself is stateless (state lives in
    `store`), so building one per call is cheap. Profile this if request
    rate climbs — likely candidate for an LRU keyed on (user_id, profile
    revision).
    """
    return AIConcierge(
        openai_client=get_async_openai_client(),
        store=DjangoConversationStore(),
        context_builder=lambda: build_specialist_context_for_actor(actor),
        tool_definitions=build_tool_definitions("string"),
        id_parser=_safe_uuid,
    )
