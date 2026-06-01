"""AIConcierge factory — wires ayla-ai-core orchestrator with Ayla deps.

Per docs/AI_CHAT_PLAN.md rev. 3 §Pipeline. The factory is the single seam
where Ayla-specific dependencies (Django ORM store, recommendation-engine
context builder, async OpenAI client, brand voice) get injected into the
generic AIConcierge from ayla-ai-core.

Wire-format note (DRF-241 Slice B):
Ayla keeps its own `ai/tools.py` (`show_specialists` / `specialist_id`)
because the public API spec v2.0 § AI ASSISTANT and the mobile contract
were written against those names. The bot uses bot-Формула naming
(`show_masters` / `master_id`) and is not migrating to shared dispatch
this quarter (Phase 2.4 added `recommend_services` not in shared).
ayla-ai-core 0.6.0 introduced `tool_dispatcher` DI for exactly this case:
each consumer keeps its own dispatcher + handlers, the orchestrator stays
generic.

`AIConcierge` itself is stateless — one instance per request is fine. The
context-builder + dispatcher form a small closure over `actor` so the
recommendation engine's geo / history filters and ownership checks work
the same way the previous local pipeline did.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID

from ayla_ai_core import (
    AIConcierge,
    AYLA_MARKETPLACE_VOICE,
    SpecialistCandidate,
    SpecialistContext,
    build_specialist_context_from_candidates,
    render_system_prompt,
)

from ai.application.services.specialist_context_builder import (
    SpecialistContext as LocalSpecialistContext,
    SpecialistContextBuilder,
)
from ai.services.llm_client import get_async_openai_client
from ai.stores import DjangoConversationStore
from ai.tools import TOOL_DEFINITIONS
from ai.tools_handlers import dispatch_tool_call as ayla_dispatch_tool_call

if TYPE_CHECKING:  # pragma: no cover
    from users.models import User


__all__ = [
    "GLOBAL_TENANT_SENTINEL",
    "build_specialist_context_for_actor",
    "get_concierge_for",
    "render_ayla_system_prompt",
    "to_core_specialist_context",
]


# Stable fallback when the request has no tenant context (anonymous
# chat without X-Tenant header). ayla-ai-core v0.7.0+ rejects empty
# string — this sentinel keeps anti-hallucination frozenset semantics
# intact while making the no-tenant case greppable. Same semantic
# value across both repos so cross-service traces are comparable.
GLOBAL_TENANT_SENTINEL = "global"


logger = logging.getLogger(__name__)


def to_core_specialist_context(
    local: LocalSpecialistContext,
    *,
    tenant_id: str,
) -> SpecialistContext[UUID]:
    """Convert local recommendation-engine output → ayla-ai-core context.

    The local DTO carries scoring metadata (match_reasons, distance_km,
    score) that's useful for the system prompt but not for AIConcierge's
    anti-hallucination layer — that only needs `candidate_ids` +
    `candidate_service_ids` frozensets, which the factory below builds.

    services_preview on the local side is just names without IDs, so the
    service-level anti-hallucination on `confirm_booking` is no-op for
    Ayla today. A later slice will surface real service IDs through the
    recommendation engine so layer-2 validation kicks in.

    ``tenant_id`` — required since ayla-ai-core v0.7.0 (security
    boundary, multi-tenant scoping). Caller passes a stable non-empty
    string identifying the request scope. See ``GLOBAL_TENANT_SENTINEL``
    for the no-tenant fallback used in anonymous chat.
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
    return build_specialist_context_from_candidates(
        candidates, tenant_id=tenant_id,
    )


def build_specialist_context_for_actor(actor: "User") -> LocalSpecialistContext:
    """Build the per-request candidate set carrying recommendation metadata.

    Returns the **local** SpecialistContext (with score, distance, reasons)
    so handlers in `ai/tools_handlers.py` keep producing rich `action_data`
    (specifically `handle_show_specialists` reads `match_reasons`). The
    factory derives the ayla-ai-core core context from this object before
    handing it to AIConcierge.
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
    return builder.build(
        client_id=client_id, client_lat=lat, client_lon=lon, city=city,
    )


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


def get_concierge_for(
    actor: "User",
    *,
    tenant_id: str = GLOBAL_TENANT_SENTINEL,
) -> AIConcierge:
    """Build a per-request AIConcierge instance bound to this actor.

    The local `SpecialistContext` (with reasons / score / distance) is
    built once per request inside `context_builder` and shared with
    `tool_dispatcher` via a tiny shared dict. The dispatcher closes over
    `actor` to thread `client_id` into Ayla's local handlers — anonymous
    users get a clarification fallback for `show_appointments`, same
    semantics as before AIConcierge wiring.

    ``tenant_id`` — caller resolves from ``request.tenant.id`` (set by
    TenantContextMiddleware) and falls back to ``GLOBAL_TENANT_SENTINEL``
    when no tenant context is available. Required by ayla-ai-core
    v0.7.0+ to partition the candidate frozensets per tenant scope.
    """
    if not tenant_id:
        # Belt-and-suspenders. ayla-ai-core would raise ValueError on
        # an empty string further down, but failing here gives a more
        # localised stack trace.
        raise ValueError(
            "tenant_id must be a non-empty stable identifier; pass "
            "GLOBAL_TENANT_SENTINEL for the no-tenant fallback."
        )
    # Shared per-request slot — context_builder writes the local context,
    # tool_dispatcher reads it. AIConcierge calls context_builder before
    # dispatcher within send_message(), so the read is always populated.
    state: dict[str, LocalSpecialistContext | None] = {"local_context": None}

    def context_builder() -> SpecialistContext[UUID]:
        local = build_specialist_context_for_actor(actor)
        state["local_context"] = local
        return to_core_specialist_context(local, tenant_id=tenant_id)

    def tool_dispatcher(tool_call, _core_context):
        local_context = state["local_context"]
        if local_context is None:  # pragma: no cover — defensive
            logger.error("ai.dispatcher.missing_local_context")
            local_context = LocalSpecialistContext(candidates=[])

        name = getattr(tool_call.function, "name", "") or ""
        raw_args = getattr(tool_call.function, "arguments", "") or "{}"
        try:
            args = json.loads(raw_args)
        except (json.JSONDecodeError, ValueError):
            args = {}

        client_id = (
            actor.id if not getattr(actor, "is_guest", False) else None
        )
        return ayla_dispatch_tool_call(
            name, args,
            context=local_context,
            client_id=client_id,
        )

    return AIConcierge(
        openai_client=get_async_openai_client(),
        store=DjangoConversationStore(),
        context_builder=context_builder,
        tool_definitions=TOOL_DEFINITIONS,
        tool_dispatcher=tool_dispatcher,
    )
