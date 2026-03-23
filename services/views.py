from django_filters.rest_framework import DjangoFilterBackend, FilterSet, filters
from rest_framework import permissions, viewsets
from rest_framework.filters import OrderingFilter

from users.permissions import IsProApp, IsSpecialist

from .models import Service, ServiceCategory
from .serializers import (
    ServiceCategorySerializer,
    ServicePublicDetailSerializer,
    ServicePublicListSerializer,
    ServiceSerializer,
)


class ServiceCategoryViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/v1/categories/ — public categories with filtering."""
    serializer_class = ServiceCategorySerializer
    permission_classes = [permissions.AllowAny]

    def get_queryset(self):
        qs = ServiceCategory.objects.filter(is_active=True)
        parent_id = self.request.query_params.get('parent_id')
        if parent_id == 'root' or parent_id is None:
            qs = qs.filter(parent__isnull=True)
        else:
            qs = qs.filter(parent_id=parent_id)
        return qs.prefetch_related('services')


class ServiceViewSet(viewsets.ModelViewSet):
    """Pro only — CRUD услуг мастера."""
    serializer_class = ServiceSerializer
    permission_classes = [permissions.IsAuthenticated, IsProApp, IsSpecialist]

    def get_queryset(self):
        return Service.objects.filter(
            specialist=self.request.user.specialist_profile,
        )

    def perform_create(self, serializer):
        serializer.save(
            specialist=self.request.user.specialist_profile,
        )


class ServicePublicFilter(FilterSet):
    """Filters for public service search."""
    min_price = filters.NumberFilter(field_name="price", lookup_expr='gte')
    max_price = filters.NumberFilter(field_name="price", lookup_expr='lte')
    name = filters.CharFilter(field_name="name", lookup_expr='icontains')

    class Meta:
        model = Service
        fields = [
            'name', 'min_price', 'max_price',
            'duration_minutes', 'specialist', 'category',
        ]


class ServicePublicViewSet(viewsets.ReadOnlyModelViewSet):
    """Public read-only API for Client App — search and browse services."""
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = ServicePublicFilter
    ordering_fields = ['price', 'name', 'created_at']
    ordering = ['sort_order', 'name']

    def get_queryset(self):
        return (
            Service.objects
            .filter(is_active=True)
            .select_related(
                'category',
                'specialist',
                'specialist__user',
            )
        )

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return ServicePublicDetailSerializer
        return ServicePublicListSerializer
