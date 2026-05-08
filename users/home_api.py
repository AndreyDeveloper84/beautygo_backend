"""Home Screen aggregated endpoint — DRF-110.

Single ``GET /api/v1/home/`` for the Ayla client app to populate the
chat-first home in one round-trip. Five sections per spec:

  upcoming_appointments      — next 3 PENDING/CONFIRMED/AWAITING_PAYMENT
  favorite_specialists       — top 5 from Favorite model (Phase 6 — DRF-72)
  popular_categories         — top 6 by specialist count, cached 1h
  nearby_specialists         — top 6 via RecommendationEngine (lat/lon from query)
  recent_activity            — last 5 COMPLETED appointments

Per-section limits are project constants — frontend codegens against
them so changing one is a contract change.

## Caching strategy

Only ``popular_categories`` is cached (TTL 1h, key ``home:popular_categories``)
because it's user-agnostic and changes only with catalog growth. The
other sections are per-user and depend on time/location, caching them
adds complexity for marginal speedup. RecommendationEngine internally
caches ``nearby_specialists`` with its own 5-minute TTL keyed on filter
hash (DRF-105) — that path is already covered.

## Pagination / sectioning

The endpoint is intentionally **not** paginated — it's a fixed-size
home view. Mobile fetches everything once on app launch and treats
each section as a card. If the client wants more (e.g. all favorites),
they navigate to a dedicated screen which calls a paginated endpoint
(``/api/v1/specialists/`` with filters).
"""
from __future__ import annotations

import logging
from typing import Any

from django.core.cache import cache as default_cache
from django.db.models import Count, Q
from django.utils import timezone
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import permissions, serializers as drf_serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from appointments.models import Appointment
from services.models import ServiceCategory
from users.permissions import IsClient, IsClientApp
from users.response import success_response

logger = logging.getLogger(__name__)


# Section limits — locked here, mobile codegens against these.
LIMIT_UPCOMING = 3
LIMIT_FAVORITES = 5
LIMIT_CATEGORIES = 6
LIMIT_NEARBY = 6
LIMIT_RECENT = 5

CACHE_KEY_POPULAR_CATEGORIES = "home:popular_categories"
CACHE_TTL_POPULAR_CATEGORIES = 3600  # 1 hour


# Statuses that mean "this booking is still ahead of the client".
_UPCOMING_STATUSES = (
    Appointment.Status.PENDING,
    Appointment.Status.AWAITING_PAYMENT,
    Appointment.Status.CONFIRMED,
)


class HomeView(APIView):
    """``GET /api/v1/home/`` 🟢 client-only.

    Query params:
      lat: float (optional) — client latitude for ``nearby_specialists``
      lon: float (optional) — client longitude for ``nearby_specialists``

    Returns five sections wrapped in the standard ``{"data": ...}`` envelope.
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]

    @extend_schema(
        responses={200: inline_serializer(
            name="HomeResponse",
            fields={
                "upcoming_appointments": drf_serializers.ListField(child=drf_serializers.DictField()),
                "favorite_specialists": drf_serializers.ListField(child=drf_serializers.DictField()),
                "popular_categories": drf_serializers.ListField(child=drf_serializers.DictField()),
                "nearby_specialists": drf_serializers.ListField(child=drf_serializers.DictField()),
                "recent_activity": drf_serializers.ListField(child=drf_serializers.DictField()),
            },
        )},
        description="Aggregated home-screen payload — 5 sections in one round-trip.",
    )
    def get(self, request: Request) -> Response:
        user = request.user
        lat, lon = self._parse_geo(request)

        payload = {
            "upcoming_appointments": self._upcoming_appointments(user),
            "favorite_specialists": self._favorite_specialists(user),
            "popular_categories": self._popular_categories(),
            "nearby_specialists": self._nearby_specialists(user, lat, lon),
            "recent_activity": self._recent_activity(user),
        }
        return success_response(payload)

    # ------------------------------------------------------------------
    # parsing
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_geo(request: Request) -> tuple[float | None, float | None]:
        try:
            lat = float(request.query_params["lat"])
            lon = float(request.query_params["lon"])
        except (KeyError, ValueError, TypeError):
            return None, None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None, None
        return lat, lon

    # ------------------------------------------------------------------
    # sections
    # ------------------------------------------------------------------
    @staticmethod
    def _upcoming_appointments(user) -> list[dict[str, Any]]:
        now = timezone.now()
        qs = (
            Appointment.objects
            .filter(client=user, start_datetime__gte=now)
            .filter(status__in=_UPCOMING_STATUSES)
            .select_related("specialist", "service")
            .order_by("start_datetime")[:LIMIT_UPCOMING]
        )
        return [
            {
                "id": str(a.id),
                "start_datetime": a.start_datetime.isoformat(),
                "end_datetime": a.end_datetime.isoformat(),
                "status": a.status,
                "service": {
                    "id": str(a.service_id),
                    "name": a.snapshot_service_name or a.service.name,
                    "price": str(a.snapshot_price or a.price),
                },
                "specialist": {
                    "id": str(a.specialist_id),
                    "display_name": a.specialist.display_name,
                    "avatar_url": (
                        a.specialist.avatar.url
                        if a.specialist.avatar else None
                    ),
                },
            }
            for a in qs
        ]

    @staticmethod
    def _favorite_specialists(user) -> list[dict[str, Any]]:
        """Top-N most recently favourited specialists for the home card.

        Mirrors the SpecialistListItem shape from /favorites/specialists/
        but trimmed for the card view (no services_preview /
        services_count to keep the home payload light). Anonymous users
        return empty since favourites require auth — the home view
        accepts unauth users for the catalog.
        """
        if not getattr(user, "is_authenticated", False):
            return []
        from .models import SpecialistProfile

        specialists = (
            SpecialistProfile.objects
            .filter(favorited_by__user=user)
            .order_by("-favorited_by__created_at")[:LIMIT_FAVORITES]
        )
        return [
            {
                "id": str(s.id),
                "display_name": s.display_name,
                "avatar": s.avatar.url if s.avatar else None,
                "rating": float(s.rating or 0),
                "reviews_count": s.reviews_count,
            }
            for s in specialists
        ]

    def _popular_categories(self) -> list[dict[str, Any]]:
        cached = default_cache.get(CACHE_KEY_POPULAR_CATEGORIES)
        if cached is not None:
            return cached

        qs = (
            ServiceCategory.objects
            .annotate(
                specialists_count=Count(
                    "services__specialist",
                    filter=Q(services__is_active=True),
                    distinct=True,
                ),
            )
            .order_by("-specialists_count", "sort_order", "name")[:LIMIT_CATEGORIES]
        )
        result = [
            {
                "id": str(c.id),
                "name": c.name,
                "icon": c.icon,
                "specialists_count": c.specialists_count,
            }
            for c in qs
        ]
        default_cache.set(
            CACHE_KEY_POPULAR_CATEGORIES, result, timeout=CACHE_TTL_POPULAR_CATEGORIES,
        )
        return result

    @staticmethod
    def _nearby_specialists(
        user, lat: float | None, lon: float | None,
    ) -> list[dict[str, Any]]:
        from ai.application.services.recommendation_engine import (
            RecommendationEngine,
            RecommendationQuery,
        )

        # If client didn't share geo, fall back to top-rated (RecommendationEngine
        # treats None lat/lon as neutral 0.5 distance score, so rating dominates).
        city = None
        profile = getattr(user, "profile", None)
        if profile is not None:
            city = getattr(profile, "city", "") or None

        engine = RecommendationEngine()
        result = engine.recommend(
            RecommendationQuery(
                client_id=user.id,
                client_lat=lat,
                client_lon=lon,
                city=city,
                limit=LIMIT_NEARBY,
            ),
        )
        return [
            {
                "id": str(s.id),
                "display_name": s.display_name,
                "rating": float(s.rating),
                "reviews_count": s.reviews_count,
                "address": s.address,
                "distance_km": s.distance_km,
                "services_preview": s.services_preview,
                "match_reasons": s.match_reasons,
            }
            for s in result.candidates
        ]

    @staticmethod
    def _recent_activity(user) -> list[dict[str, Any]]:
        """Recently COMPLETED appointments — useful for "записывайтесь снова"
        cards on home. Reviews surface elsewhere; we keep this section
        single-shape to avoid mobile having to multiplex types."""
        qs = (
            Appointment.objects
            .filter(client=user, status=Appointment.Status.COMPLETED)
            .select_related("specialist", "service")
            .order_by("-start_datetime")[:LIMIT_RECENT]
        )
        return [
            {
                "id": str(a.id),
                "start_datetime": a.start_datetime.isoformat(),
                "service_name": a.snapshot_service_name or a.service.name,
                "specialist": {
                    "id": str(a.specialist_id),
                    "display_name": a.specialist.display_name,
                    "avatar_url": (
                        a.specialist.avatar.url
                        if a.specialist.avatar else None
                    ),
                },
            }
            for a in qs
        ]
