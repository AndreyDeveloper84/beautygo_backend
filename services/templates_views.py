"""Service Templates API (DRF-198).

Exposes:
    GET /api/v1/service-templates/?category_id=<uuid>&lat=&lon=&region=
    GET /api/v1/service-templates/regions/

Used on the specialist onboarding screen to show template suggestions with
recommended regional pricing.
"""
from __future__ import annotations

from uuid import UUID

from django.core.cache import cache
from rest_framework import permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsProApp
from users.response import error_response, success_response

from .models import RegionalPricing, ServiceCategory, ServiceTemplate
from .pricing import get_region_key

CACHE_TTL_SECONDS = 3600  # 1 hour (AC)
CACHE_KEY_PREFIX = 'service_templates'


def _parse_float(raw: str | None) -> float | None:
    if raw is None or raw == '':
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _serialize_template(
    tpl: ServiceTemplate,
    prices_by_template: dict[str, RegionalPricing],
    fallback_prices_by_template: dict[str, RegionalPricing],
) -> dict:
    price = prices_by_template.get(str(tpl.id))
    if price is None:
        price = fallback_prices_by_template.get(str(tpl.id))
    return {
        'id': str(tpl.id),
        'name': tpl.name,
        'name_short': tpl.name_short,
        'duration_default': tpl.duration_default,
        'duration_min': tpl.duration_min,
        'duration_max': tpl.duration_max,
        'is_popular': tpl.is_popular,
        'recommended_price_min': (
            int(price.price_min) if price is not None else None
        ),
        'recommended_price_max': (
            int(price.price_max) if price is not None else None
        ),
    }


class ServiceTemplatesListView(APIView):
    """🟣 Pro only — templates with recommended regional pricing."""
    permission_classes = [permissions.IsAuthenticated, IsProApp]

    def get(self, request: Request) -> Response:
        category_id_raw = request.query_params.get('category_id')
        if not category_id_raw:
            return error_response(
                'VALIDATION_ERROR',
                'category_id is required',
                status_code=400,
            )
        try:
            category_id = UUID(category_id_raw)
        except (TypeError, ValueError):
            return error_response(
                'VALIDATION_ERROR',
                'category_id must be a valid UUID',
                status_code=400,
            )

        if not ServiceCategory.objects.filter(pk=category_id).exists():
            return error_response(
                'NOT_FOUND', 'Category not found', status_code=404,
            )

        region_key = self._resolve_region(request)
        cache_key = f"{CACHE_KEY_PREFIX}:{category_id}:{region_key}"
        cached = cache.get(cache_key)
        if cached is not None:
            return success_response(cached)

        payload = self._build_payload(category_id, region_key)
        cache.set(cache_key, payload, timeout=CACHE_TTL_SECONDS)
        return success_response(payload)

    @staticmethod
    def _resolve_region(request: Request) -> str:
        explicit_region = (request.query_params.get('region') or '').strip()
        if explicit_region:
            return explicit_region
        lat = _parse_float(request.query_params.get('lat'))
        lon = _parse_float(request.query_params.get('lon'))
        return get_region_key(lat=lat, lon=lon)

    @staticmethod
    def _build_payload(category_id: UUID, region_key: str) -> dict:
        templates = list(
            ServiceTemplate.objects
            .filter(category_id=category_id)
            .order_by('-is_popular', 'sort_order', 'name')
        )
        template_ids = [t.id for t in templates]

        prices = list(
            RegionalPricing.objects.filter(
                template_id__in=template_ids, region_key=region_key,
            )
        )
        prices_by_template = {str(p.template_id): p for p in prices}

        fallback_prices_by_template: dict[str, RegionalPricing] = {}
        if region_key != RegionalPricing.DEFAULT_REGION_KEY:
            missing_ids = [
                tid for tid in template_ids
                if str(tid) not in prices_by_template
            ]
            if missing_ids:
                fallback_prices = RegionalPricing.objects.filter(
                    template_id__in=missing_ids,
                    region_key=RegionalPricing.DEFAULT_REGION_KEY,
                )
                fallback_prices_by_template = {
                    str(p.template_id): p for p in fallback_prices
                }

        region_name = _resolve_region_name(region_key, prices)

        return {
            'region': region_key,
            'region_name': region_name,
            'templates': [
                _serialize_template(
                    t, prices_by_template, fallback_prices_by_template,
                )
                for t in templates
            ],
        }


def _resolve_region_name(
    region_key: str, prices: list[RegionalPricing],
) -> str:
    """Return a human-readable region name using DB rows or a static fallback."""
    for price in prices:
        if price.region_name:
            return price.region_name
    row = (
        RegionalPricing.objects
        .filter(region_key=region_key)
        .values_list('region_name', flat=True)
        .first()
    )
    if row:
        return row
    if region_key == RegionalPricing.DEFAULT_REGION_KEY:
        return 'Другой город'
    return region_key.capitalize()


class SupportedRegionsView(APIView):
    """🟣 Pro only — list of supported regions for manual city selection."""
    permission_classes = [permissions.IsAuthenticated, IsProApp]

    def get(self, request: Request) -> Response:
        rows = (
            RegionalPricing.objects
            .values('region_key', 'region_name')
            .distinct()
            .order_by('region_key')
        )
        # Ensure the default option always appears, even on an empty DB.
        keys = {r['region_key'] for r in rows}
        payload = [
            {'key': r['region_key'], 'name': r['region_name']}
            for r in rows
        ]
        if RegionalPricing.DEFAULT_REGION_KEY not in keys:
            payload.append({
                'key': RegionalPricing.DEFAULT_REGION_KEY,
                'name': 'Другой город',
            })
        return success_response(payload)
