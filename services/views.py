from __future__ import annotations

from django.db.models import Count, Prefetch, Q, QuerySet
from django_filters.rest_framework import DjangoFilterBackend, FilterSet, filters
from rest_framework import permissions, serializers, status, viewsets
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from users.permissions import IsProApp, IsSpecialist
from users.response import error_response, success_response

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

    def get_queryset(self) -> QuerySet[ServiceCategory]:
        """Return active categories with specialist counts and prefetched children."""
        children_prefetch = Prefetch(
            'children',
            queryset=ServiceCategory.objects.filter(is_active=True)
            .annotate(
                specialists_count=Count(
                    'services__specialist',
                    filter=Q(services__is_active=True),
                    distinct=True,
                ),
            )
            .order_by('sort_order', 'name'),
        )
        qs = (
            ServiceCategory.objects
            .filter(is_active=True)
            .annotate(
                specialists_count=Count(
                    'services__specialist',
                    filter=Q(services__is_active=True),
                    distinct=True,
                ),
            )
            .prefetch_related(children_prefetch)
            .order_by('sort_order', 'name')
        )
        parent_id = self.request.query_params.get('parent_id')
        if parent_id == 'root' or parent_id is None:
            qs = qs.filter(parent__isnull=True)
        else:
            qs = qs.filter(parent_id=parent_id)
        return qs


class ServiceViewSet(viewsets.ModelViewSet):
    """Pro only — CRUD услуг мастера."""
    serializer_class = ServiceSerializer
    permission_classes = [permissions.IsAuthenticated, IsProApp, IsSpecialist]

    def get_queryset(self) -> QuerySet[Service]:
        """Return services belonging to the authenticated specialist."""
        return Service.objects.filter(
            specialist=self.request.user.specialist_profile,
        ).select_related('category')

    def perform_create(self, serializer: ServiceSerializer) -> None:
        """Assign the current specialist as the service owner on create."""
        serializer.save(
            specialist=self.request.user.specialist_profile,
        )

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(qs, many=True)
        return success_response(serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )
        self.perform_create(serializer)
        return success_response(serializer.data, status_code=201)

    def retrieve(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance)
        return success_response(serializer.data)

    def update(self, request, *args, **kwargs):
        partial = kwargs.pop('partial', False)
        instance = self.get_object()
        serializer = self.get_serializer(
            instance, data=request.data, partial=partial,
        )
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Invalid input",
                details=serializer.errors, status_code=400,
            )
        serializer.save()
        return success_response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        kwargs['partial'] = True
        return self.update(request, *args, **kwargs)

    def destroy(self, request, *args, **kwargs):
        instance = self.get_object()

        # Check for active appointments
        from appointments.domain.value_objects import ACTIVE_BOOKING_STATUSES
        active_count = instance.appointments.filter(
            status__in=[s.value for s in ACTIVE_BOOKING_STATUSES],
        ).count()

        if active_count > 0:
            return error_response(
                "HAS_APPOINTMENTS",
                f"Нельзя удалить услугу с активными записями ({active_count})",
                status_code=409,
            )

        instance.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


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

    def get_queryset(self) -> QuerySet[Service]:
        """Return active services with related specialist and category."""
        return (
            Service.objects
            .filter(is_active=True)
            .select_related(
                'category',
                'specialist',
                'specialist__user',
            )
        )

    def get_serializer_class(self) -> type[serializers.Serializer]:
        """Return detail serializer for retrieve, list serializer otherwise."""
        if self.action == 'retrieve':
            return ServicePublicDetailSerializer
        return ServicePublicListSerializer
