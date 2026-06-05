"""Internal Bearer catalog read — services + categories (#1016 S2).

Service-to-service surface the Ayla bot reads to mirror the services
catalog. Reuses the public read-only viewsets (same querysets,
serializers, filters) and only swaps the auth boundary to
``IsInternalBearer`` (Bearer <AYLA_INTERNAL_API_TOKEN>), since the bot
sends neither a mobile JWT nor X-App-Type.
"""
from __future__ import annotations

from users.permissions import IsInternalBearer

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
