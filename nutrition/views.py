"""Nutrition API views — Food Scanner endpoint (Slice 2).

Routes:
  POST /api/v1/nutrition/scan/   → FoodScanView.post

Slices 3-5 will add:
  POST /nutrition/food-log/, GET /nutrition/summary/
  POST /nutrition/water/, etc.

X-App-Type: client only — Pro app doesn't show nutrition features.
"""
from __future__ import annotations

import logging

from django.core.files.base import ContentFile
from rest_framework import permissions, status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from users.permissions import IsClient, IsClientApp
from users.response import error_response, success_response

from nutrition.models import FoodScan
from nutrition.serializers import (
    FoodLogCreateSerializer,
    FoodLogEntrySerializer,
    FoodScanResponseSerializer,
    ScanRequestSerializer,
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
        try:
            log = FoodLogService().create(CreateFoodLogInput(
                user_id=request.user.id,
                portion_multiplier=v["portion_multiplier"],
                meal_type=v["meal_type"],
                scan_id=v.get("scan_id"),
                dish_name=v.get("dish_name"),
                logged_at=v.get("logged_at"),
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
