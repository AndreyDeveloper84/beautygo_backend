"""Specialists public API — list and search for Client App."""
from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any

from django.core import exceptions as django_exceptions
from django.db.models import Q, QuerySet
from django_filters.rest_framework import DjangoFilterBackend, FilterSet, filters
from rest_framework import permissions, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.filters import OrderingFilter
from rest_framework.response import Response

from services.catalog_reads import (
    annotate_catalog_services_count,
    catalog_services_for,
    catalog_services_prefetch,
)
from services.models import Service
from .models import SpecialistProfile

logger = logging.getLogger(__name__)


# Активная бронируемая связка канонического каталога, от
# ``SpecialistProfile``. Вынесено на уровень модуля: ``FilterSet``
# разбирает атрибуты класса как объявления фильтров.
_ACTIVE_CANONICAL = Q(
    specialist_services__is_active=True,
    specialist_services__salon_service__is_active=True,
)


# --- Mixins ---

class DistanceMixin:
    """Mixin for serializers that need distance calculation from request lat/lon."""

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


# --- Serializers ---

class ServicePreviewSerializer(serializers.Serializer):
    """Top-3 services preview for specialist card.

    Читает ``services.catalog_reads.CatalogService`` — единую форму для
    ОБОИХ слоёв каталога, а не модель ``Service``. Легаси-таблица на
    пилоте пуста целиком (замер 2026-08-30: 0 строк против 292
    канонических связок), и превью приходило пустым у каждого мастера.

    ``id`` — ключ, который принимает бронирование этой услуги: для
    маркетплейса ``Service.id``, для салонного каталога
    ``SalonService.id`` (его разбирает ``resolve_bookable_service``).
    """
    id = serializers.UUIDField(read_only=True)
    name = serializers.CharField(read_only=True)
    price = serializers.DecimalField(
        max_digits=10, decimal_places=2, read_only=True,
    )
    duration_minutes = serializers.IntegerField(
        read_only=True, allow_null=True,
    )


class SpecialistListSerializer(DistanceMixin, serializers.ModelSerializer):
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

    def get_services_preview(self, obj: SpecialistProfile) -> list[dict[str, Any]]:
        """Top-3 active services from BOTH catalog layers.

        Uses the queryset's prefetch (``catalog_services_prefetch``) —
        no extra query.
        """
        return ServicePreviewSerializer(
            catalog_services_for(obj)[:3], many=True,
        ).data

    def get_services_count(self, obj: SpecialistProfile) -> int:
        """Active services count (uses annotation, no extra query)."""
        return getattr(obj, 'active_services_count', 0)


class ServiceFullSerializer(serializers.Serializer):
    """Full service details for specialist detail view.

    Та же смена источника, что и у ``ServicePreviewSerializer`` — поля
    контракта сохранены один в один. ``image`` у салонного каталога
    всегда ``None``: картинки там нет по схеме, и выдумывать её нельзя.
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
    image = serializers.CharField(
        source='image_url', read_only=True, allow_null=True,
    )
    category = serializers.UUIDField(
        source='category_id', read_only=True, allow_null=True,
    )
    category_name = serializers.CharField(read_only=True, allow_null=True)
    is_active = serializers.SerializerMethodField()
    sort_order = serializers.IntegerField(read_only=True)

    def get_is_active(self, obj) -> bool:
        # ``catalog_services_for`` отдаёт только активные — поле остаётся
        # в контракте, но врать ему нечем.
        return True


class SpecialistDetailSerializer(DistanceMixin, serializers.ModelSerializer):
    """Full specialist profile for detail/card view."""
    user_id = serializers.UUIDField(source='user.id')
    services = serializers.SerializerMethodField()
    services_count = serializers.SerializerMethodField()
    distance_km = serializers.SerializerMethodField()
    reviews_summary = serializers.SerializerMethodField()
    recent_reviews = serializers.SerializerMethodField()
    working_hours = serializers.SerializerMethodField()
    portfolio = serializers.SerializerMethodField()

    class Meta:
        model = SpecialistProfile
        fields = [
            'id', 'user_id', 'display_name', 'avatar', 'bio',
            'experience_years', 'address',
            'location_lat', 'location_lng',
            'rating', 'reviews_count', 'is_available',
            'services', 'services_count', 'distance_km',
            'reviews_summary', 'recent_reviews', 'working_hours',
            'portfolio', 'created_at',
        ]

    def get_services(self, obj: SpecialistProfile) -> list[dict[str, Any]]:
        """All active services from BOTH catalog layers (prefetched)."""
        return ServiceFullSerializer(
            catalog_services_for(obj), many=True,
        ).data

    def get_services_count(self, obj: SpecialistProfile) -> int:
        """Active services count (uses annotation, no extra query)."""
        return getattr(obj, 'active_services_count', 0)

    def get_reviews_summary(self, obj: SpecialistProfile) -> dict[str, Any]:
        # TODO(DRF-reviews): implement when Review model is added
        return {
            'average': float(obj.rating),
            'count': obj.reviews_count,
            'distribution': {1: 0, 2: 0, 3: 0, 4: 0, 5: 0},
        }

    def get_recent_reviews(self, obj: SpecialistProfile) -> list:
        # TODO(DRF-reviews): implement when Review model is added
        return []

    def get_portfolio(self, obj: SpecialistProfile) -> list[dict[str, Any]]:
        """Public portfolio listing (DRF-194). Ordering matches the model's
        Meta.ordering — sort_order asc, created_at asc as a tiebreaker."""
        return [
            {
                'id': str(item.id),
                'image_url': item.image.url if item.image else "",
                'sort_order': item.sort_order,
            }
            for item in obj.portfolio.all()
        ]

    def get_working_hours(self, obj: SpecialistProfile) -> list:
        """Specialist weekly schedule from prefetched SpecialistWorkingHours.

        Uses ``obj.working_hours.all()`` to consume the queryset's prefetch
        instead of issuing a fresh query per specialist (N+1). Sorting in
        Python — the prefetch returns the related set already loaded.
        """
        hours = sorted(
            obj.working_hours.all(),
            key=lambda wh: wh.day_of_week,
        )
        return [
            {
                'day_of_week': wh.day_of_week,
                'day_name': wh.get_day_of_week_display(),
                'is_working_day': wh.is_working_day,
                'start_time': wh.start_time.strftime('%H:%M') if wh.start_time else None,
                'end_time': wh.end_time.strftime('%H:%M') if wh.end_time else None,
                'break_start': wh.break_start.strftime('%H:%M') if wh.break_start else None,
                'break_end': wh.break_end.strftime('%H:%M') if wh.break_end else None,
            }
            for wh in hours
        ]


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
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


def compute_specialist_day_slots(
    specialist: SpecialistProfile,
    *,
    service_id: str | None,
    date_param: str | None,
    allow_salon_fallback: bool = False,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Compute available booking slots for one specialist/service/day.

    Shared by the public mobile endpoint (``GET /specialists/{id}/slots/``)
    and the internal Bearer endpoint (``GET /api/v1/internal/specialists/
    {id}/slots/``, #1016) so both honour the same
    SpecialistWorkingHours + SpecialistTimeOff + active-booking logic
    via ``AvailabilityQueryService``.

    ``allow_salon_fallback`` (AMD-019, INTERNAL surface only): resolve
    ``service_id`` through the shared resolver — marketplace ``Service``
    first, then ``SalonService`` with an active SpecialistService link
    in the current tenant. The public path keeps the marketplace-only
    lookup (observable behaviour unchanged).

    ⚠️ ОТКРЫТАЯ ДЫРА, НЕ ЗАКРЫТАЯ ЗДЕСЬ (S3-EMPTY, 2026-08-30).
    Публичный каталог (``SpecialistViewSet``) теперь ПОКАЗЫВАЕТ салонные
    услуги — иначе на пилоте он показывал пустоту. Но эта ветка на
    салонный ``service_id`` по-прежнему отдаёт 404: граница AMD-019
    («резолвер только для внутренней поверхности») — решение владельца от
    2026-07-21, и расширять её самовольно нельзя. То есть клиентское
    приложение увидит услугу, слоты по которой не отдаются.

    Это СОЗНАТЕЛЬНО видимая поломка вместо прежней невидимой пустоты:
    404 попадает в логи и в жалобу, а пустой каталог не попадал никуда —
    ровно поэтому дефект дожил до сегодня. Закрывается решением
    владельца о снятии границы AMD-019 для публичной поверхности.

    На боевом пилоте эта дыра не проявляется: бот и Mini App ходят через
    внутреннюю поверхность, где ``allow_salon_fallback=True``.

    Returns ``(payload, None)`` on success or ``(None, error)`` on
    failure, where ``error`` carries an ``_status`` key the caller pops
    to set the HTTP status. ``payload`` matches the established mobile
    contract: ``{"date": "YYYY-MM-DD", "slots": [<ISO-8601 local>, ...]}``.
    """
    from zoneinfo import ZoneInfo

    from appointments.application.dto import GetAvailabilityDTO
    from appointments.application.services.availability_query_service import (
        AvailabilityQueryService,
    )

    if not service_id:
        return None, {
            '_status': 400, 'code': 'MISSING_PARAM',
            'message': 'service_id is required',
        }

    duration_override: int | None = None
    buffer_override: int | None = None
    if allow_salon_fallback:
        # AMD-019 — shared resolver (services/service_resolver.py), the
        # SAME one CreateBookingService uses. Unavailable → the same
        # 404 shape as the marketplace miss below (no existence leak).
        from services.service_resolver import (
            ServiceUnavailableForSpecialistError,
            resolve_bookable_service,
        )
        try:
            resolved = resolve_bookable_service(
                service_id=service_id, specialist=specialist,
            )
        except (
            ServiceUnavailableForSpecialistError,
            ValueError,
            django_exceptions.ValidationError,
        ):
            return None, {
                '_status': 404, 'code': 'NOT_FOUND',
                'message': 'Service not found',
            }
        duration_override = resolved.duration_minutes
        buffer_override = resolved.buffer_after_minutes
        resolved_service_id = resolved.service_id
    else:
        try:
            service = Service.objects.get(
                id=service_id, specialist=specialist, is_active=True,
            )
        except (Service.DoesNotExist, ValueError, django_exceptions.ValidationError):
            return None, {
                '_status': 404, 'code': 'NOT_FOUND',
                'message': 'Service not found',
            }
        duration_override = service.duration_minutes
        buffer_override = service.buffer_after_minutes
        resolved_service_id = service.pk

    specialist_tz = ZoneInfo(specialist.timezone)
    try:
        slot_date: date = (
            datetime.strptime(date_param, '%Y-%m-%d').date()
            if date_param
            else datetime.now(tz=specialist_tz).date()
        )
    except ValueError:
        return None, {
            '_status': 400, 'code': 'INVALID_PARAM',
            'message': 'date must be YYYY-MM-DD',
        }

    result = AvailabilityQueryService().get_day_availability(
        GetAvailabilityDTO(
            specialist_id=specialist.pk,
            target_date=slot_date,
            service_id=resolved_service_id,
        ),
        duration_override=duration_override,
        buffer_override=buffer_override,
    )

    # Mobile contract: slots as ISO-8601 strings in the specialist's
    # local timezone (mobile parses them as Date objects).
    if not result.is_working_day:
        available: list[str] = []
    else:
        available = [
            slot.start_at.astimezone(specialist_tz).isoformat()
            for slot in result.slots
        ]

    return {'date': slot_date.isoformat(), 'slots': available}, None


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

    # Каждый фильтр ниже — ИЛИ по двум слоям каталога. Раньше все они
    # join-или только легаси ``services__`` и на пилоте (0 легаси-строк
    # против 292 канонических связок) отдавали пустой каталог на любой
    # выбор категории, услуги или цены.

    def filter_by_category(self, queryset: QuerySet, name: str, value: Any) -> QuerySet:
        """Категория: своя у салонной услуги, иначе — категория шаблона.

        Фолбэк, а не объединение (см. ``services.catalog_reads``).
        """
        canonical = _ACTIVE_CANONICAL & (
            Q(specialist_services__salon_service__category_id=value)
            | Q(
                specialist_services__salon_service__category_id__isnull=True,
                specialist_services__salon_service__template__category_id=value,
            )
        )
        return queryset.filter(
            (Q(services__category_id=value) & Q(services__is_active=True))
            | canonical
        ).distinct()

    def filter_by_service(self, queryset: QuerySet, name: str, value: Any) -> QuerySet:
        """``service_id`` — маркетплейсный ``Service.id`` ИЛИ ``SalonService.id``.

        Тот же ключ, что принимает бронирование
        (``services.service_resolver.resolve_bookable_service``).
        """
        return queryset.filter(
            (Q(services__id=value) & Q(services__is_active=True))
            | (
                _ACTIVE_CANONICAL
                & Q(specialist_services__salon_service_id=value)
            )
        ).distinct()

    def filter_by_min_price(self, queryset: QuerySet, name: str, value: Any) -> QuerySet:
        return queryset.filter(
            (Q(services__price__gte=value) & Q(services__is_active=True))
            | (
                _ACTIVE_CANONICAL
                & Q(specialist_services__price__gte=value)
            )
        ).distinct()

    def filter_by_max_price(self, queryset: QuerySet, name: str, value: Any) -> QuerySet:
        return queryset.filter(
            (Q(services__price__lte=value) & Q(services__is_active=True))
            | (
                _ACTIVE_CANONICAL
                & Q(specialist_services__price__lte=value)
            )
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

    def get_serializer_class(self) -> type:
        if self.action == 'retrieve':
            return SpecialistDetailSerializer
        return SpecialistListSerializer

    @action(detail=True, methods=['get'], url_path='services')
    def services(self, request, pk=None):
        """GET /specialists/{id}/services/ — all active services."""
        specialist = self.get_object()
        return Response(
            ServiceFullSerializer(
                catalog_services_for(specialist), many=True,
            ).data,
        )

    @action(detail=True, methods=['get'], url_path='slots')
    def slots(self, request, pk=None) -> Response:
        """
        GET /specialists/{id}/slots/?service_id=<uuid>&date=<YYYY-MM-DD>

        Returns available booking slots for the given specialist, service
        and date. Defaults to today (in specialist's timezone) if date not
        supplied.

        Delegates to ``compute_specialist_day_slots`` (shared with the
        internal Bearer surface, #1016) so the specialist's real
        SpecialistWorkingHours + SpecialistTimeOff + active bookings are
        respected (fixes ln-624 #1).
        """
        specialist = self.get_object()
        payload, error = compute_specialist_day_slots(
            specialist,
            service_id=request.query_params.get('service_id'),
            date_param=request.query_params.get('date'),
        )
        if error is not None:
            return Response({'error': error}, status=error.pop('_status'))
        return Response(payload)

    # GET /specialists/{id}/reviews/ — wired directly in users/specialists_urls.py
    # to SpecialistReviewsView (reviews app), avoiding the cross-app late import
    # and the per-action permission_classes override that used to live here.

    def get_queryset(self) -> QuerySet:
        qs = (
            SpecialistProfile.objects
            .filter(
                status=SpecialistProfile.ProfileStatus.ACTIVE,
                is_available=True,
                user__is_active=True,
            )
            .select_related('user')
            .prefetch_related(
                *catalog_services_prefetch(), 'portfolio', 'working_hours',
            )
        )
        # Счётчик по обоим слоям каталога — раньше считались только
        # легаси-строки, то есть на пилоте ноль у каждого мастера.
        qs = annotate_catalog_services_count(qs)

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
