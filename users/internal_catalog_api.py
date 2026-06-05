"""Internal Bearer catalog read — specialists + slots (#1016 S2).

Service-to-service surface the nationwide Ayla bot's
``apps/integrations/ayla/`` REST client reads to mirror the catalog
and compute availability. Ayla backend stays the single source of
truth (ADR-0009); the bot only reads.

All endpoints reuse the public ``SpecialistViewSet`` — same queryset
(active + available specialists), serializers, ``slots`` and
``services`` actions — swapping only the auth boundary: ``Bearer
<AYLA_INTERNAL_API_TOKEN>`` via ``IsInternalBearer`` instead of the
mobile JWT + X-App-Type stack. Catalog-shaped (read/lookup across many
records, no per-user write), so ``IsInternalBearer`` is the right
class — no ``X-External-User-ID`` second factor needed.
"""
from __future__ import annotations

from users.permissions import IsInternalBearer
from users.specialists_api import SpecialistViewSet


class InternalSpecialistViewSet(SpecialistViewSet):
    """GET /api/v1/internal/specialists/ (+ /{id}/, /{id}/slots/,
    /{id}/services/) — Bearer-authed catalog mirror for the bot.

    Inherits list/retrieve/slots/services + the catalog queryset from
    ``SpecialistViewSet`` unchanged. Only the auth boundary differs.
    """

    # Disable DRF's default JWTAuthentication: the bot ships
    # Authorization: Bearer <AYLA_INTERNAL_API_TOKEN>, which is NOT a
    # JWT — leaving JWTAuthentication in place would 401-reject before
    # IsInternalBearer runs (same rationale as masters/internal #92).
    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
