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

from django.http import Http404
from rest_framework.decorators import action
from rest_framework.response import Response

from users.models import SpecialistProfile
from users.permissions import IsInternalBearer
from users.specialists_api import (
    SpecialistViewSet,
    compute_specialist_day_slots,
)


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

    @action(detail=True, methods=['get'], url_path='slots')
    def slots(self, request, pk=None) -> Response:
        """AMD-019 — internal slots resolve ``service_id`` through the
        shared resolver (marketplace Service OR SalonService with an
        active SpecialistService link in the tenant).

        The public action stays untouched: its service filter
        (marketplace M2M) would 404 a SalonService id BEFORE any slot
        math, so here the specialist is fetched through the base
        queryset (same active+available constraints, no service filter)
        and the service is validated by the resolver itself.
        """
        try:
            specialist = self.get_queryset().get(pk=pk)
        except SpecialistProfile.DoesNotExist:
            raise Http404
        payload, error = compute_specialist_day_slots(
            specialist,
            service_id=request.query_params.get('service_id'),
            date_param=request.query_params.get('date'),
            allow_salon_fallback=True,
        )
        if error is not None:
            return Response({'error': error}, status=error.pop('_status'))
        return Response(payload)
