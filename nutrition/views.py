"""Nutrition API views.

Routes:
  POST   /api/v1/nutrition/scan/        → FoodScanView (Slice 2)
  POST   /api/v1/nutrition/food-log/    → FoodLogCreateView (Slice 3b)
  GET    /api/v1/nutrition/summary/     → NutritionSummaryView (Slice 3c)
  POST   /api/v1/nutrition/water/       → WaterLogCreateView (Slice 4)
  DELETE /api/v1/nutrition/water/{id}/  → WaterLogDeleteView (Slice 4)
  GET    /api/v1/nutrition/water/today/ → WaterTodayView (Slice 4)

X-App-Type: client only — Pro app doesn't show nutrition features.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone as dt_tz
from uuid import UUID

from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from users.permissions import IsClient, IsClientApp, IsServiceAccount
from users.response import error_response, success_response
from users.services import InvalidExternalUserIDError, resolve_external_user

from nutrition.models import FoodScan, WaterLog
from nutrition.serializers import (
    FoodLogCreateSerializer,
    FoodLogEntrySerializer,
    FoodScanResponseSerializer,
    NutritionSummaryQuerySerializer,
    NutritionSummaryResponseSerializer,
    ScanRequestSerializer,
    WaterLogCreateSerializer,
    WaterLogResponseSerializer,
    WaterTodayResponseSerializer,
)
from nutrition.services.food_log_service import (
    CreateFoodLogInput,
    DishNotRecognizedError,
    FoodLogService,
    InvalidInputError,
    ScanNotOwnedError,
)
from nutrition.services.food_scanner_router import (
    AllProvidersFailedError,
    FoodScannerRouter,
)
from nutrition.services.nutrition_lookup import NutritionLookup
from nutrition.services.nutrition_summary_service import NutritionSummaryService
from nutrition.services.water_service import (
    WaterLogCreatedResponse,
    WaterService,
)

logger = logging.getLogger(__name__)


class FoodScanView(APIView):
    """POST /api/v1/nutrition/scan/."""

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan"

    def post(self, request: Request) -> Response:
        serializer = ScanRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )

        image_file = serializer.validated_data["image"]
        portion_multiplier = serializer.validated_data.get("portion_multiplier") or 1.0

        # Read once — providers and storage both want bytes; ImageField
        # streams from request body so we must materialise before two
        # reads. 10 MiB cap is enforced in the serializer.
        image_bytes = image_file.read()

        scan = FoodScan(user=request.user)
        scan.image.save(
            f"{scan.id}.jpg",
            ContentFile(image_bytes),
            save=False,
        )

        router = FoodScannerRouter()
        try:
            outcome = router.scan(
                image_bytes,
                portion_multiplier=portion_multiplier,
                user=request.user,
            )
        except AllProvidersFailedError as exc:
            # Per spec v2.0 §FOOD SCANNER, distinguish "vendor worked but
            # image isn't food / not recognisable" (400 FOOD_NOT_RECOGNIZED)
            # from "vendor unreachable" (503 FOOD_API_UNAVAILABLE).
            if exc.is_low_confidence_only:
                error_code = "FOOD_NOT_RECOGNIZED"
                http_status = status.HTTP_400_BAD_REQUEST
                msg = "Не удалось распознать блюдо на фото"
            else:
                error_code = "FOOD_API_UNAVAILABLE"
                http_status = status.HTTP_503_SERVICE_UNAVAILABLE
                msg = "Сервис распознавания временно недоступен"

            scan.error_code = error_code
            scan.error_message = str(exc)[:500]
            scan.save()
            logger.warning(
                "nutrition.scan.all_providers_failed user=%s code=%s err=%s",
                request.user.id, error_code, exc,
            )
            return error_response(
                error_code, msg, status_code=http_status,
            )

        scan.dish_name = outcome.result.dish_name
        scan.confidence = outcome.result.confidence
        scan.portion_g = outcome.result.portion_g
        scan.ingredients = outcome.result.ingredients
        scan.provider_used = outcome.result.provider
        scan.provider_fallback_from = (
            outcome.primary_provider_name
            if outcome.primary_failed_with
            else ""
        )
        scan.latency_ms = outcome.result.latency_ms
        scan.raw_response = outcome.result.raw_response

        # Slice 3a: seed-only lookup. Misses leave nutrition=null and the
        # mobile client shows "уточните порцию вручную". OFF/USDA HTTP
        # fallback ships in 3a'.
        facts = NutritionLookup().lookup(
            outcome.result.dish_name,
            ingredients=outcome.result.ingredients,
            portion_g=outcome.result.portion_g,
        )
        scan.nutrition = facts.to_dict() if facts is not None else None

        scan.save()

        return success_response(
            FoodScanResponseSerializer(scan).data,
            status_code=status.HTTP_200_OK,
        )


class InternalFoodScanView(APIView):
    """POST /api/v1/nutrition/internal/scan/ — service-to-service food scan.

    DRF-246. Mirrors `FoodScanView` but authenticates via shared service token
    (`X-Service-Token`) and resolves the actor from `X-External-User-ID`
    (e.g. `bot:12345`). Used by the MAX bot to scan on behalf of a BotUser
    that has not yet been migrated to a real Ayla account.

    Lazy ProxyUser creation: first call for a given external_user_id creates
    a `User(is_proxy=True, role='client')`. Subsequent calls reuse it.
    """

    permission_classes = [IsServiceAccount]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def post(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )

        serializer = ScanRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )

        image_file = serializer.validated_data["image"]
        portion_multiplier = serializer.validated_data.get("portion_multiplier") or 1.0

        image_bytes = image_file.read()

        scan = FoodScan(user=user)
        scan.image.save(
            f"{scan.id}.jpg",
            ContentFile(image_bytes),
            save=False,
        )

        router = FoodScannerRouter()
        try:
            outcome = router.scan(
                image_bytes,
                portion_multiplier=portion_multiplier,
                user=user,
            )
        except AllProvidersFailedError as exc:
            if exc.is_low_confidence_only:
                error_code = "FOOD_NOT_RECOGNIZED"
                http_status = status.HTTP_400_BAD_REQUEST
                msg = "Не удалось распознать блюдо на фото"
            else:
                error_code = "FOOD_API_UNAVAILABLE"
                http_status = status.HTTP_503_SERVICE_UNAVAILABLE
                msg = "Сервис распознавания временно недоступен"

            scan.error_code = error_code
            scan.error_message = str(exc)[:500]
            scan.save()
            logger.warning(
                "nutrition.internal_scan.all_providers_failed user=%s ext=%s code=%s err=%s",
                user.id, external_user_id, error_code, exc,
            )
            return error_response(error_code, msg, status_code=http_status)

        scan.dish_name = outcome.result.dish_name
        scan.confidence = outcome.result.confidence
        scan.portion_g = outcome.result.portion_g
        scan.ingredients = outcome.result.ingredients
        scan.provider_used = outcome.result.provider
        scan.provider_fallback_from = (
            outcome.primary_provider_name
            if outcome.primary_failed_with
            else ""
        )
        scan.latency_ms = outcome.result.latency_ms
        scan.raw_response = outcome.result.raw_response

        facts = NutritionLookup().lookup(
            outcome.result.dish_name,
            ingredients=outcome.result.ingredients,
            portion_g=outcome.result.portion_g,
        )
        scan.nutrition = facts.to_dict() if facts is not None else None

        scan.save()

        return success_response(
            FoodScanResponseSerializer(scan).data,
            status_code=status.HTTP_200_OK,
        )


class FoodLogCreateView(APIView):
    """POST /api/v1/nutrition/food-log/ — log a meal to the diary.

    Per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION. Two creation
    paths handled by FoodLogService — see service module for details.
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_log"

    def post(self, request: Request) -> Response:
        serializer = FoodLogCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )
        v = serializer.validated_data
        # Caller provides X-Idempotency-Key (UUID) on retries to prevent
        # duplicate diary entries when mobile re-sends after a flaky
        # network. Absent header = no de-dup, every POST creates new.
        idempotency_key = request.META.get("HTTP_X_IDEMPOTENCY_KEY") or None
        try:
            log = FoodLogService().create(CreateFoodLogInput(
                user_id=request.user.id,
                portion_multiplier=v["portion_multiplier"],
                meal_type=v["meal_type"],
                scan_id=v.get("scan_id"),
                dish_name=v.get("dish_name"),
                logged_at=v.get("logged_at"),
                idempotency_key=idempotency_key,
            ))
        except InvalidInputError as exc:
            return error_response(
                "VALIDATION_ERROR", str(exc),
            )
        except ScanNotOwnedError:
            # Use 404 to avoid leaking whether the scan exists at all.
            return error_response(
                "SCAN_NOT_FOUND",
                "Сканирование не найдено",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except DishNotRecognizedError as exc:
            logger.info(
                "nutrition.food_log.not_recognized user=%s err=%s",
                request.user.id, exc,
            )
            return error_response(
                "FOOD_NOT_RECOGNIZED",
                "Не удалось определить макросы блюда",
            )

        return success_response(
            FoodLogEntrySerializer(log).data,
            status_code=status.HTTP_201_CREATED,
        )


class NutritionSummaryView(APIView):
    """GET /api/v1/nutrition/summary/?date=YYYY-MM-DD — daily diary summary.

    Per Notion API Spec v2.0 §FOOD SCANNER+NUTRITION. ``date`` defaults
    to today (UTC). See NutritionSummaryService for day-boundary
    semantics and stubbed fields (water + vitamins).
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "nutrition_summary"

    def get(self, request: Request) -> Response:
        q = NutritionSummaryQuerySerializer(data=request.query_params)
        if not q.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидный параметр date — ожидается YYYY-MM-DD",
                details=q.errors,
            )
        day = q.validated_data.get("date") or datetime.now(dt_tz.utc).date()

        summary = NutritionSummaryService().summary(
            user_id=request.user.id, day=day,
        )
        return success_response(
            NutritionSummaryResponseSerializer(summary).data,
            status_code=status.HTTP_200_OK,
        )


# ---------------------------------------------------------------------------
# Water tracker (Slice 4)
# ---------------------------------------------------------------------------


class WaterLogCreateView(APIView):
    """POST /api/v1/nutrition/water/ — log a glass of water."""

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "water"

    def post(self, request: Request) -> Response:
        serializer = WaterLogCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )

        log = WaterLog.objects.create(
            user=request.user,
            amount_ml=serializer.validated_data["amount_ml"],
            logged_at=timezone.now(),
        )
        agg = WaterService().aggregate_for_today(request.user.id)
        # Per spec v2.0 §FOOD SCANNER+NUTRITION POST /nutrition/water:
        # Response 200, not 201. Glasses are user-counter increments,
        # not first-class created resources, so spec returns 200.
        return success_response(
            WaterLogResponseSerializer(
                WaterLogCreatedResponse(aggregate=agg, log_id=log.id)
            ).data,
            status_code=status.HTTP_200_OK,
        )


class WaterLogDeleteView(APIView):
    """DELETE /api/v1/nutrition/water/{id}/ — undo a glass.

    Returns the same WaterLogResponse shape so the mobile UI can
    update its progress ring without a follow-up GET.
    """

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "water"

    def delete(self, request: Request, pk: UUID) -> Response:
        try:
            log = WaterLog.objects.get(id=pk, user=request.user)
        except WaterLog.DoesNotExist:
            # 404 (no existence leak) — same pattern as scan ownership.
            return error_response(
                "NOT_FOUND",
                "Запись не найдена",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        deleted_id = log.id
        # Aggregate for the deleted log's day (UTC), not "today" — so
        # the mobile UI showing yesterday's diary updates correctly when
        # the user undoes a glass from a past day.
        deleted_day = log.logged_at.astimezone(dt_tz.utc).date()
        log.delete()
        agg = WaterService().aggregate_for_day(request.user.id, deleted_day)
        return success_response(
            WaterLogResponseSerializer(
                WaterLogCreatedResponse(aggregate=agg, log_id=deleted_id)
            ).data,
            status_code=status.HTTP_200_OK,
        )


class WaterTodayView(APIView):
    """GET /api/v1/nutrition/water/today/ — list today's glasses."""

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "water"

    def get(self, request: Request) -> Response:
        today = WaterService().today_logs(request.user.id)
        return success_response(
            WaterTodayResponseSerializer(today).data,
            status_code=status.HTTP_200_OK,
        )
