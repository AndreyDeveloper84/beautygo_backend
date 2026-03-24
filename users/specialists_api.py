"""Specialists public API — list and search for Client App."""

import logging
import math

from django_filters.rest_framework import DjangoFilterBackend, FilterSet, filters
from rest_framework import permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from services.models import Service
from .models import SpecialistProfile

logger = logging.getLogger(__name__)


# --- Serializers ---

class ServicePreviewSerializer(serializers.ModelSerializer):
    """Top-3 services preview for specialist card."""
    class Meta:
        model = Service
        fields = ['id', 'name', 'price', 'duration_minutes']


class SpecialistListSerializer(serializers.ModelSerializer):
    """Specialist card for catalog listing."""
    user_id = serializers.UUIDField(source='user.id')
    services_preview = serializers.SerializerMethodField()
    services_count = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = SpecialistProfile
        fields = [
            'id', 'user_id', 'display_name', 'avatar', 'bio',
            'experience_years', 'address',
            'location_lat', 'location_lng',
            'status', 'rating', 'reviews_count', 'is_available',
            'services_preview', 'services_count', 'distance_km',
        ]

    def get_services_preview(self, obj):
        """Top-3 active services by sort_order."""
        services = (
            obj.services
            .filter(is_active=True)
            .order_by('sort_order', 'name')[:3]
        )
        return ServicePreviewSerializer(services, many=True).data

    def get_services_count(self, obj):
        return obj.services.filter(is_active=True).count()

    def get_distance_km(self, obj):
        """Distance from client's location (if provided in request)."""
        request = self.context.get('request')
        if not request:
            return None
        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        if not lat or not lon or not obj.location_lat or not obj.location_lng:
            return None
        try:
            return _haversine(
                float(lat), float(lon),
                float(obj.location_lat), float(obj.location_lng),
            )
        except (ValueError, TypeError):
            return None


class ServiceFullSerializer(serializers.ModelSerializer):
    """Full service details for specialist detail view."""
    category_name = serializers.CharField(
        source='category.name', default=None,
    )

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'description', 'price', 'duration_minutes',
            'image', 'category', 'category_name', 'is_active', 'sort_order',
        ]


class SpecialistDetailSerializer(serializers.ModelSerializer):
    """Full specialist profile for detail/card view."""
    user_id = serializers.UUIDField(source='user.id')
    services = serializers.SerializerMethodField()
    services_count = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    reviews_summary = serializers.SerializerMethodField()
    recent_reviews = serializers.SerializerMethodField()
    working_hours = serializers.SerializerMethodField()

    class Meta:
        model = SpecialistProfile
        fields = [
            'id', 'user_id', 'display_name', 'avatar', 'bio',
            'experience_years', 'address',
            'location_lat', 'location_lng',
            'rating', 'reviews_count', 'is_available',
            'services', 'services_count', 'distance_km',
            'reviews_summary', 'recent_reviews', 'working_hours',
            'created_at',
        ]

    def get_services(self, obj):
        """All active services, ordered by sort_order."""
        services = (
            obj.services
            .filter(is_active=True)
            .select_related('category')
            .order_by('sort_order', 'name')
        )
        return ServiceFullSerializer(services, many=True).data

    def get_services_count(self, obj):
        return obj.services.filter(is_active=True).count()

    def get_distance_km(self, obj):
        request = self.context.get('request')
        if not request:
            return None
        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        if not lat or not lon or not obj.location_lat or not obj.location_lng:
            return None
        try:
            return _haversine(
                float(lat), float(lon),
                float(obj.location_lat), float(obj.location_lng),
            )
        except (ValueError, TypeError):
            return None

    def get_reviews_summary(self, obj):
        """Reviews summary — stub until Review model exists."""
        return {
            'average': float(obj.rating),
            'count': obj.reviews_count,
            'distribution': {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
        }

    def get_recent_reviews(self, obj):
        """Recent reviews — stub until Review model exists."""
        return []

    def get_working_hours(self, obj):
        """Working hours — stub until WorkingHours model exists."""
        return []


def _haversine(lat1, lon1, lat2, lon2):
    """Calculate distance in km between two points using Haversine formula."""
    R = 6371  # Earth radius in km
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 1)


# --- Filters ---

class SpecialistFilter(FilterSet):
    """Filters for specialist search."""
    category_id = filters.UUIDFilter(
        method='filter_by_category',
        label='Category ID',
    )
    service_id = filters.UUIDFilter(
        method='filter_by_service',
        label='Service ID',
    )
    min_rating = filters.NumberFilter(
        field_name='rating', lookup_expr='gte',
    )
    is_available = filters.BooleanFilter(field_name='is_available')
    min_price = filters.NumberFilter(
        method='filter_by_min_price',
        label='Min price',
    )
    max_price = filters.NumberFilter(
        method='filter_by_max_price',
        label='Max price',
    )

    class Meta:
        model = SpecialistProfile
        fields = ['min_rating', 'is_available']

    def filter_by_category(self, queryset, name, value):
        return queryset.filter(
            services__category_id=value,
            services__is_active=True,
        ).distinct()

    def filter_by_service(self, queryset, name, value):
        return queryset.filter(
            services__id=value,
            services__is_active=True,
        ).distinct()

    def filter_by_min_price(self, queryset, name, value):
        return queryset.filter(
            services__price__gte=value,
            services__is_active=True,
        ).distinct()

    def filter_by_max_price(self, queryset, name, value):
        return queryset.filter(
            services__price__lte=value,
            services__is_active=True,
        ).distinct()


# --- ViewSet ---

class SpecialistViewSet(viewsets.ReadOnlyModelViewSet):
    """GET /api/v1/specialists/ — public specialist search for Client App."""
    serializer_class = SpecialistListSerializer
    permission_classes = [permissions.IsAuthenticated]
    filter_backends = [DjangoFilterBackend, OrderingFilter]
    filterset_class = SpecialistFilter
    ordering_fields = ['rating', 'reviews_count', 'experience_years']
    ordering = ['-rating']

    def get_serializer_class(self):
        if self.action == 'retrieve':
            return SpecialistDetailSerializer
        return SpecialistListSerializer

    @action(detail=True, methods=['get'], url_path='services')
    def services(self, request, pk=None):
        """GET /specialists/{id}/services/ — all active services."""
        specialist = self.get_object()
        services = (
            specialist.services
            .filter(is_active=True)
            .select_related('category')
            .order_by('sort_order', 'name')
        )
        return Response(
            ServiceFullSerializer(services, many=True).data,
        )

    @action(detail=True, methods=['get'], url_path='slots')
    def slots(self, request, pk=None):
        """GET /specialists/{id}/slots/ — available slots (stub)."""
        self.get_object()  # 404 if not found
        return Response([])

    @action(detail=True, methods=['get'], url_path='reviews')
    def reviews(self, request, pk=None):
        """GET /specialists/{id}/reviews/ — reviews (stub)."""
        self.get_object()  # 404 if not found
        return Response({'results': [], 'count': 0})

    def get_queryset(self):
        qs = (
            SpecialistProfile.objects
            .filter(
                status=SpecialistProfile.ProfileStatus.ACTIVE,
                is_available=True,
                user__is_active=True,
            )
            .select_related('user')
            .prefetch_related('services')
        )

        # Geo filter: lat, lon, radius (km)
        lat = self.request.query_params.get('lat')
        lon = self.request.query_params.get('lon')
        radius = self.request.query_params.get('radius')

        if lat and lon and radius:
            try:
                lat_f = float(lat)
                lon_f = float(lon)
                radius_f = float(radius)
                # Approximate bounding box filter (fast)
                lat_delta = radius_f / 111.0
                lon_delta = radius_f / (
                    111.0 * math.cos(math.radians(lat_f))
                )
                qs = qs.filter(
                    location_lat__gte=lat_f - lat_delta,
                    location_lat__lte=lat_f + lat_delta,
                    location_lng__gte=lon_f - lon_delta,
                    location_lng__lte=lon_f + lon_delta,
                )
            except (ValueError, TypeError):
                pass

        return qs
