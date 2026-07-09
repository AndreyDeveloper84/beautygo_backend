"""Internal Bearer catalog read — services + categories (#1016 S2).

Service-to-service surface the Ayla bot reads to mirror the services
catalog. Reuses the public read-only viewsets (same querysets,
serializers, filters) and only swaps the auth boundary to
``IsInternalBearer`` (Bearer <AYLA_INTERNAL_API_TOKEN>), since the bot
sends neither a mobile JWT nor X-App-Type.
"""
from __future__ import annotations

from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import viewsets

from users.permissions import IsInternalBearer

from .models import SalonService, SpecialistService
from .serializers import (
    SalonServiceInternalSerializer,
    SpecialistServiceInternalSerializer,
)
from .views import ServiceCategoryViewSet, ServicePublicViewSet


class InternalServiceViewSet(ServicePublicViewSet):
    """GET /api/v1/internal/services/ (+ /{id}/) — Bearer catalog mirror."""

    # See InternalSpecialistViewSet for the empty-authentication_classes
    # rationale (bot bearer is not a JWT).
    authentication_classes: list = []
    permission_classes = [IsInternalBearer]


class InternalServiceCategoryViewSet(ServiceCategoryViewSet):
    """GET /api/v1/internal/services/categories/ — Bearer category list."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]


# --- S3A canonical catalog mirror (#1044 / #200) ---


class InternalSalonServiceViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/v1/internal/catalog/salon-services/ (+ /{id}/)."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    serializer_class = SalonServiceInternalSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['tenant', 'template', 'is_active']
    queryset = (
        SalonService.objects
        .select_related('tenant', 'template', 'category')
        .order_by('created_at')
    )


class InternalSpecialistServiceViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/v1/internal/catalog/specialist-services/ (+ /{id}/).

    The bookable mirror — stable ``id`` is the bot's booking key.
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    serializer_class = SpecialistServiceInternalSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['tenant', 'specialist', 'salon_service', 'is_active']
    queryset = (
        SpecialistService.objects
        .select_related(
            'salon_service', 'salon_service__template', 'specialist', 'tenant',
        )
        .order_by('created_at')
    )
