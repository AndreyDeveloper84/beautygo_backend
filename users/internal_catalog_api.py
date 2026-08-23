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

### Tenant scope (DRF-1313)

``/api/v1/internal/`` is excluded from ``TenantContextMiddleware``, so
``request.tenant`` is always ``None`` here and the tenant cannot be
derived from the request the way it is on the ``/api/v1/tenants/me/…``
tree (DRF-1297 / PR #240). The tenant is therefore taken as an explicit
``?tenant=<uuid>`` query filter — exactly the shape the neighbouring
``/internal/catalog/specialist-services/`` handle already uses.

The filter **narrows only**. It is not an authorization boundary and
does not pretend to be one: the bearer already grants the whole
internal tree, and a query param neither widens nor narrows *that*.
What it buys is a per-tenant snapshot the catalog mirror can key on.
Without it the list is the full active roster of the platform and
every syncing tenant claims the same masters — DRF-1313: the five
masters of four salons all landed under ``mkt-spatrium``, and three
salons became unbookable.

Optional rather than required, for one reason: the consumer has to be
able to deploy *after* this change. A required param would 400 every
mirror run in the window between the two deploys and break the one
salon that works today. A tenant-less list is instead logged at
WARNING, so the tenant-blind pull is visible in ops rather than
silent.

Responses carry ``tenant`` so the consumer can verify that the scope
it asked for actually held — the same cross-tenant guard the
``specialist-services`` rows already enable.
"""
from __future__ import annotations

import logging

from django.http import Http404
from django_filters.rest_framework import filters
from rest_framework import serializers
from rest_framework.decorators import action
from rest_framework.response import Response

from users.models import SpecialistProfile
from users.permissions import IsInternalBearer
from users.specialists_api import (
    SpecialistDetailSerializer,
    SpecialistFilter,
    SpecialistListSerializer,
    SpecialistViewSet,
    compute_specialist_day_slots,
)

logger = logging.getLogger(__name__)


class InternalSpecialistFilter(SpecialistFilter):
    """Public specialist filters + the ``?tenant=`` scope (DRF-1313).

    Declared on a subclass rather than added to ``SpecialistFilter`` so the
    public Client App catalog keeps exactly the filter surface it has today —
    this param exists for the internal mirror and for nothing else.

    ``UUIDFilter`` validates: a malformed value is a form error → **400**, not
    a silently ignored param. That matters more here than anywhere else. The
    failure this fixes *was* a filter being ignored in silence, and a typo'd
    tenant answering with the whole platform would repeat it exactly.
    """

    tenant = filters.UUIDFilter(field_name='tenant_id', label='Tenant UUID')


class _InternalTenantFieldMixin:
    """Adds the owning tenant to the internal specialist payloads.

    The public serializers stay untouched: the Client App has no use for a
    tenant id, and the internal mirror must not change what mobile renders.

    The consumer needs this to hold the upstream filter to its word. The bot's
    ``MasterService`` upsert already refuses an edge whose payload tenant
    differs from the tenant being synced; the masters upsert had no equivalent
    check and had to trust the query param blindly — which is precisely the
    trust that failed here.
    """

    tenant = serializers.UUIDField(
        source='tenant_id', read_only=True, allow_null=True,
    )


class InternalSpecialistListSerializer(
    _InternalTenantFieldMixin, SpecialistListSerializer,
):
    """List card + ``tenant``. Additive — no public field changes shape."""

    class Meta(SpecialistListSerializer.Meta):
        fields = SpecialistListSerializer.Meta.fields + ['tenant']


class InternalSpecialistDetailSerializer(
    _InternalTenantFieldMixin, SpecialistDetailSerializer,
):
    """Detail profile + ``tenant``. Additive, same as the list serializer."""

    class Meta(SpecialistDetailSerializer.Meta):
        fields = SpecialistDetailSerializer.Meta.fields + ['tenant']


class InternalSpecialistViewSet(SpecialistViewSet):
    """GET /api/v1/internal/specialists/ (+ /{id}/, /{id}/slots/,
    /{id}/services/) — Bearer-authed catalog mirror for the bot.

    Inherits list/retrieve/slots/services + the catalog queryset from
    ``SpecialistViewSet`` unchanged. The auth boundary differs, and the
    list accepts ``?tenant=<uuid>`` (see the module docstring, DRF-1313).
    """

    # Disable DRF's default JWTAuthentication: the bot ships
    # Authorization: Bearer <AYLA_INTERNAL_API_TOKEN>, which is NOT a
    # JWT — leaving JWTAuthentication in place would 401-reject before
    # IsInternalBearer runs (same rationale as masters/internal #92).
    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    filterset_class = InternalSpecialistFilter

    def get_serializer_class(self) -> type:
        if self.action == 'retrieve':
            return InternalSpecialistDetailSerializer
        return InternalSpecialistListSerializer

    def list(self, request, *args, **kwargs):
        """List, plus a loud note when the caller named no tenant at all.

        A tenant-less pull is still served (see the module docstring on why
        the param is optional), but it returns every active master on the
        platform. A catalog mirror consuming that is what made three of five
        pilot salons unbookable — and it did so without a single line in any
        log. One WARNING per such call is the cheapest possible tripwire.
        """
        if not request.query_params.get('tenant'):
            logger.warning(
                "internal.specialists.list_without_tenant path=%s — returning "
                "the full active roster of the platform. A catalog mirror "
                "consuming this attributes every master to whichever tenant "
                "syncs first (DRF-1313). Pass ?tenant=<uuid>.",
                request.path,
            )
        return super().list(request, *args, **kwargs)

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
