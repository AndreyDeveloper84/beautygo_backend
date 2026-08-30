"""Global Search API — unified search across specialists and services.

S3-EMPTY (2026-08-30) — поиск читает ОБА слоя каталога
------------------------------------------------------
Раньше и раздел «услуги», и превью услуг мастера, и join мастера по
названию услуги ходили только в легаси ``Service``. На боевом пилоте эта
таблица пуста целиком (замер: 0 строк против 292 канонических связок),
поэтому поиск по любому названию услуги молча не находил ничего — и
выглядело это как «ничего не нашлось», а не как поломка.

Читающие примитивы — в ``services.catalog_reads``: там же разрешение
категории (своя категория салона побеждает, шаблон — запасной путь) и
правило о том, какой ``id`` уезжает наружу (тот, который принимает
бронирование).

Полнотекстовый PG-ранкинг остаётся ТОЛЬКО над легаси-веткой: он
навешивается на queryset модели, а объединённая выдача — питоновский
список. Членство в выдаче и раньше определял icontains-предфильтр
(#477), ранкинг лишь переупорядочивал; теперь порядок задаёт рейтинг
мастера для обеих веток одинаково. Это осознанная потеря релевантности
ради непустого ответа, а не тихий откат.
"""
from __future__ import annotations

import logging
import math

from django.db import connection
from django.db.models import Q, QuerySet
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import permissions, serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from services.catalog_reads import (
    catalog_services_for,
    catalog_services_prefetch,
    resolved_category,
    specialist_service_text_q,
)
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
        """Топ-3 активных услуги из ОБОИХ слоёв каталога."""
        return [
            {'id': s.id, 'name': s.name, 'price': str(s.price)}
            for s in catalog_services_for(obj)[:3]
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


class SearchServiceSerializer(serializers.Serializer):
    """Service result in search response.

    Сериализует plain-dict из ``_search_services`` — форма одна для
    обоих слоёв каталога. Поля контракта сохранены один в один.
    """
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True,
    )
    duration_minutes = serializers.IntegerField(
        read_only=True, allow_null=True,
    )
    category = serializers.UUIDField(read_only=True, allow_null=True)
    category_name = serializers.CharField(read_only=True, allow_null=True)
    specialist_id = serializers.UUIDField(read_only=True)
    specialist_name = serializers.CharField(read_only=True)
    specialist_rating = serializers.DecimalField(
        max_digits=2, decimal_places=1, read_only=True, allow_null=True,
    )
    specialist_avatar = serializers.CharField(
        read_only=True, allow_null=True,
    )


# --- Search View ---

class GlobalSearchView(APIView):
    """
    GET /api/v1/search/?q=<query>&type=all&lat=&lon=&limit=10

    Unified search across specialists and services.
    Uses PostgreSQL full-text search when available, falls back to icontains for SQLite.
    """
    permission_classes = [permissions.IsAuthenticated]
    serializer_class = SearchSpecialistSerializer

    @extend_schema(
        responses={
            200: inline_serializer(
                name="GlobalSearchResponse",
                fields={
                    "specialists": SearchSpecialistSerializer(many=True),
                    "services": SearchServiceSerializer(many=True),
                    "query": serializers.CharField(),
                },
            ),
        },
    )
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
            # Превью услуг читает оба слоя каталога — без prefetch это
            # два запроса на каждую строку выдачи.
            .prefetch_related(*catalog_services_prefetch())
        )

        # #477 — pg full-text returns empty for some Cyrillic queries
        # when the russian config / dictionary isn't installed on the
        # target Postgres image (CI uses postgres:16 base which has
        # russian by default, but production images can vary). Use
        # __icontains as the canonical path; PG full-text remains as a
        # ranking layer when available. icontains is correct on Postgres
        # via the LIKE operator (Postgres ILIKE = case-insensitive
        # __icontains) — works on both dev / test / prod.
        # Услугу ищем через ``specialist_service_text_q`` — оба слоя
        # каталога. Раньше здесь стоял join только по ``services__``, и
        # на пилоте мастер по названию своей услуги не находился вовсе.
        qs = qs.filter(
            Q(display_name__icontains=q)
            | Q(bio__icontains=q)
            | specialist_service_text_q(q)
        ).distinct()
        if IS_POSTGRES:
            # Layer the PG full-text rank ordering on top for relevance,
            # but don't filter — the icontains pre-filter is the
            # source-of-truth for membership.
            qs = self._pg_rank_specialists(qs, q)

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

    def _search_services(self, q: str, limit: int) -> list[dict]:
        """Услуги из ОБОИХ слоёв каталога, одной формой.

        Легаси-ветка сохраняет прежний путь целиком, включая PG-ранкинг
        (#477: icontains — источник истины членства, ранкинг только
        переупорядочивает). Салонная ветка добирается тем же
        icontains-предикатом по названию услуги и её разрешённой
        категории.

        Возвращает список словарей, а не queryset: два слоя — две
        модели, общего queryset у них нет.
        """
        legacy_qs = (
            Service.objects
            .filter(is_active=True)
            .select_related('category', 'specialist', 'specialist__user')
            .filter(
                specialist__status=SpecialistProfile.ProfileStatus.ACTIVE,
                specialist__is_available=True,
            )
            .filter(
                Q(name__icontains=q)
                | Q(description__icontains=q)
                | Q(category__name__icontains=q)
            )
        )
        if IS_POSTGRES:
            legacy_qs = self._pg_rank_services(legacy_qs, q)
        legacy_rows = [
            self._service_row(
                service_id=svc.id,
                name=svc.name,
                description=svc.description,
                price=svc.price,
                duration_minutes=svc.duration_minutes,
                category=svc.category,
                specialist=svc.specialist,
            )
            for svc in legacy_qs.order_by('-specialist__rating')[:limit]
        ]

        salon_rows = [
            self._service_row(
                # Ключ, который принимает бронирование — ``SalonService.id``
                # (его разбирает ``resolve_bookable_service``).
                service_id=link.salon_service_id,
                name=link.salon_service.name,
                # У ``SalonService`` описания нет по схеме.
                description='',
                price=link.price,
                duration_minutes=link.resolved_duration(),
                category=resolved_category(link.salon_service),
                specialist=link.specialist,
            )
            for link in self._salon_service_matches(q, limit)
        ]

        rows = legacy_rows + salon_rows
        rows.sort(
            key=lambda row: (
                -float(row['specialist_rating'] or 0), str(row['id']),
            ),
        )
        return rows[:limit]

    @staticmethod
    def _salon_service_matches(q: str, limit: int) -> list:
        """Активные бронируемые связки, совпавшие по названию/категории.

        Категория разрешается с запасным путём через шаблон: своя
        категория салона побеждает (``services.catalog_reads``).
        """
        from services.models import SpecialistService

        category_match = (
            Q(salon_service__category__name__icontains=q)
            | Q(
                salon_service__category__isnull=True,
                salon_service__template__category__name__icontains=q,
            )
        )
        return list(
            SpecialistService.objects
            .filter(
                is_active=True,
                salon_service__is_active=True,
                specialist__status=SpecialistProfile.ProfileStatus.ACTIVE,
                specialist__is_available=True,
            )
            .filter(Q(salon_service__name__icontains=q) | category_match)
            .select_related(
                'salon_service', 'salon_service__category',
                'salon_service__template', 'salon_service__template__category',
                'specialist', 'specialist__user',
            )
            .order_by('-specialist__rating')[:limit]
        )

    @staticmethod
    def _service_row(
        *, service_id, name, description, price, duration_minutes,
        category, specialist,
    ) -> dict:
        """Одна форма строки услуги для обоих слоёв каталога."""
        avatar = getattr(specialist, 'avatar', None)
        return {
            'id': service_id,
            'name': name,
            'description': description,
            'price': price,
            'duration_minutes': duration_minutes,
            'category': category.id if category else None,
            'category_name': category.name if category else None,
            'specialist_id': specialist.id,
            'specialist_name': specialist.display_name,
            'specialist_rating': specialist.rating,
            'specialist_avatar': avatar.url if avatar else None,
        }

    @staticmethod
    def _pg_rank_specialists(qs: QuerySet, q: str) -> QuerySet:
        """PostgreSQL full-text RANKING on top of icontains pre-filter.

        Adds a `rank` annotation + orders by it for relevance. Does
        NOT filter — the icontains pre-filter is membership source
        of truth (so this is robust to russian-config availability).

        Вектор намеренно оставлен на легаси-полях: членство в выдаче
        обеспечивает предфильтр (он теперь читает оба слоя), а добавление
        второго каталожного join в сам вектор размножило бы строки под
        ``SearchRank`` и исказило веса. Мастер, найденный по салонной
        услуге, получает rank 0 и уезжает вниз выдачи — он ВИДЕН, просто
        ранжирован хуже. Это потеря релевантности, не пустота.
        """
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
            .order_by('-rank')
            .distinct()
        )

    @staticmethod
    def _pg_rank_services(qs: QuerySet, q: str) -> QuerySet:
        """PG full-text RANKING on top of icontains pre-filter.
        Mirror of _pg_rank_specialists — see that docstring."""
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
