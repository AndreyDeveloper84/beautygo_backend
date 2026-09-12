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
from users.response import success_response

from .goal_resolution import build_category_goal_index
from .models import SalonService, ServiceCategory, SpecialistService
from .serializers import (
    SalonServiceInternalSerializer,
    ServiceDirectionSerializer,
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


class InternalServiceDirectionViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/v1/internal/services/directions/ — направления для онбординга мастера (DRF-1798).

    Источник истины — корни глобальной таксономии: ``ServiceCategory`` с
    ``parent IS NULL``, ``tenant IS NULL``, ``is_active``. Салонные корни
    (``tenant`` задан) сюда не попадают: экран «Чем вы занимаетесь?» показывает
    канон, а не чью-то кураторскую копию. Шесть названий макета не
    зашиты — рантайм читает каталог (слово владельца 12.09: канон
    первичен); расхождение макета с каноном записано в карте отдельно.

    Без пагинации: корней в каноне 22, и экран показывает их все.
    """

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]
    serializer_class = ServiceDirectionSerializer
    pagination_class = None
    queryset = (
        ServiceCategory.objects
        .filter(parent__isnull=True, tenant__isnull=True, is_active=True)
        .order_by('sort_order', 'name')
    )

    def list(self, request, *args, **kwargs):  # type: ignore[override]
        """Конверт ``{"data": [...]}`` — как у остальных внутренних маршрутов."""
        serializer = self.get_serializer(self.get_queryset(), many=True)
        return success_response(serializer.data)


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

    def get_serializer_context(self) -> dict:
        """Индекс «категория → цели» строится один раз на запрос (DRF-1308).

        Без этого каждая строка страницы тянула бы связи целей отдельно —
        N+1 на каталоге из десятков услуг.
        """
        context = super().get_serializer_context()
        context['category_goal_index'] = build_category_goal_index()
        return context


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
