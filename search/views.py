"""Global Search API — unified search across specialists and services."""
from __future__ import annotations

import logging
import math

from django.db import connection
from django.db.models import Q, QuerySet
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from services.models import Service
from users.models import SpecialistProfile
from users.response import success_response

logger = logging.getLogger(__name__)

IS_POSTGRES = connection.vendor == 'postgresql'


# --- Serializers ---

class SearchSpecialistSerializer(serializers.ModelSerializer):
    """Specialist result in search response."""
    user_id = serializers.UUIDField(source='user.id')
    services_preview = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()

    class Meta:
        model = SpecialistProfile
        fields = [
            'id', 'user_id', 'display_name', 'avatar', 'bio',
            'rating', 'reviews_count', 'address',
            'location_lat', 'location_lng',
            'services_preview', 'distance_km',
        ]

    def get_services_preview(self, obj: SpecialistProfile) -> list[dict]:
        services = obj.services.filter(is_active=True).order_by('sort_order')[:3]
        return [
            {'id': s.id, 'name': s.name, 'price': str(s.price)}
            for s in services
        ]

    def get_distance_km(self, obj: SpecialistProfile) -> float | None:
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


class SearchServiceSerializer(serializers.ModelSerializer):
    """Service result in search response."""
    category_name = serializers.CharField(
        source='category.name', default=None,
    )
    specialist_id = serializers.UUIDField(source='specialist.id')
    specialist_name = serializers.CharField(source='specialist.display_name')
    specialist_rating = serializers.DecimalField(
        source='specialist.rating', max_digits=2, decimal_places=1,
    )
    specialist_avatar = serializers.ImageField(
        source='specialist.avatar', default=None,
    )

    class Meta:
        model = Service
        fields = [
            'id', 'name', 'description', 'price', 'duration_minutes',
            'category', 'category_name',
            'specialist_id', 'specialist_name',
            'specialist_rating', 'specialist_avatar',
        ]


# --- Search View ---

class GlobalSearchView(APIView):
    """
    GET /api/v1/search/?q=<query>&type=all&lat=&lon=&limit=10

    Unified search across specialists and services.
    Uses PostgreSQL full-text search when available, falls back to icontains for SQLite.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request) -> Response:
        q = request.query_params.get('q', '').strip()
        if not q or len(q) < 2:
            return success_response({
                'specialists': [],
                'services': [],
                'query': q,
            })

        search_type = request.query_params.get('type', 'all')
        limit = min(int(request.query_params.get('limit', 10)), 50)

        result = {}

        if search_type in ('all', 'specialists'):
            specialists = self._search_specialists(q, request, limit)
            result['specialists'] = SearchSpecialistSerializer(
                specialists, many=True, context={'request': request},
            ).data

        if search_type in ('all', 'services'):
            services = self._search_services(q, limit)
            result['services'] = SearchServiceSerializer(
                services, many=True,
            ).data

        result['query'] = q
        return success_response(result)

    def _search_specialists(
        self, q: str, request: Request, limit: int,
    ) -> QuerySet:
        qs = (
            SpecialistProfile.objects
            .filter(
                status=SpecialistProfile.ProfileStatus.ACTIVE,
                is_available=True,
                user__is_active=True,
            )
            .select_related('user')
        )

        if IS_POSTGRES:
            qs = self._pg_search_specialists(qs, q)
        else:
            qs = qs.filter(
                Q(display_name__icontains=q)
                | Q(bio__icontains=q)
                | Q(services__name__icontains=q)
                | Q(services__category__name__icontains=q)
            ).distinct()

        # Geo sorting if lat/lon provided
        lat = request.query_params.get('lat')
        lon = request.query_params.get('lon')
        if lat and lon:
            try:
                lat_f, lon_f = float(lat), float(lon)
                specialists = list(qs[:limit * 3])  # overfetch for sorting
                specialists.sort(
                    key=lambda s: _haversine(
                        lat_f, lon_f,
                        float(s.location_lat or 0), float(s.location_lng or 0),
                    ) if s.location_lat else float('inf')
                )
                return specialists[:limit]
            except (ValueError, TypeError):
                pass

        return qs.order_by('-rating')[:limit]

    def _search_services(self, q: str, limit: int) -> QuerySet:
        qs = (
            Service.objects
            .filter(is_active=True)
            .select_related('category', 'specialist', 'specialist__user')
            .filter(
                specialist__status=SpecialistProfile.ProfileStatus.ACTIVE,
                specialist__is_available=True,
            )
        )

        if IS_POSTGRES:
            qs = self._pg_search_services(qs, q)
        else:
            qs = qs.filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(category__name__icontains=q)
            )

        return qs.order_by('-specialist__rating')[:limit]

    @staticmethod
    def _pg_search_specialists(qs: QuerySet, q: str) -> QuerySet:
        """PostgreSQL full-text search for specialists."""
        from django.contrib.postgres.search import (
            SearchQuery, SearchRank, SearchVector,
        )
        vector = (
            SearchVector('display_name', weight='A')
            + SearchVector('bio', weight='B')
            + SearchVector('services__name', weight='A')
            + SearchVector('services__category__name', weight='B')
        )
        query = SearchQuery(q, config='russian')
        return (
            qs
            .annotate(rank=SearchRank(vector, query))
            .filter(rank__gt=0.01)
            .order_by('-rank')
            .distinct()
        )

    @staticmethod
    def _pg_search_services(qs: QuerySet, q: str) -> QuerySet:
        """PostgreSQL full-text search for services."""
        from django.contrib.postgres.search import (
            SearchQuery, SearchRank, SearchVector,
        )
        vector = (
            SearchVector('name', weight='A')
            + SearchVector('description', weight='B')
            + SearchVector('category__name', weight='B')
        )
        query = SearchQuery(q, config='russian')
        return (
            qs
            .annotate(rank=SearchRank(vector, query))
            .filter(rank__gt=0.01)
            .order_by('-rank')
        )


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in km between two points."""
    R = 6371
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
