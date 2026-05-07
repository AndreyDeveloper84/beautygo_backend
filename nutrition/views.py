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
from datetime import datetime, timedelta, timezone as dt_tz
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

from nutrition.models import Beverage, FoodScan, WaterLog
from nutrition.serializers import (
    BeverageCatalogItemSerializer,
    FoodLogCreateSerializer,
    FoodLogEntrySerializer,
    FoodScanResponseSerializer,
    NutritionProfileUpsertSerializer,
    NutritionSummaryQuerySerializer,
    NutritionSummaryResponseSerializer,
    PatternDetectionResponseSerializer,
    ReturningSuccessResponseSerializer,
    ScanRequestSerializer,
    WaterEntryCreateSerializer,
    WaterEntryResponseSerializer,
    WaterLogCreateSerializer,
    WaterLogResponseSerializer,
    WaterTodayResponseSerializer,
    WaterTodayResponseSerializerV3,
)
from nutrition.services.pattern_detection_service import detect_patterns
from nutrition.services.returning_success_service import detect_returning_success
from nutrition.services.profile_upsert_service import (
    get_profile_response,
    upsert_profile,
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
from nutrition.services.deficit_hints import build_deficit_hint
from nutrition.services.nutrition_lookup import NutritionLookup
from nutrition.services.nutrition_summary_service import NutritionSummaryService
from nutrition.services.water_entry_service import (
    CreateWaterInput,
    EntryNotFoundError,
    InvalidMlError,
    RestoreWindowExpiredError,
    UnknownBeverageError,
    WaterEntryService,
    RESTORE_WINDOW_MINUTES,
)
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
        caption = serializer.validated_data.get("caption") or ""

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
                caption=caption,
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
        caption = serializer.validated_data.get("caption") or ""

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
                caption=caption,
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


def _create_food_log_for(user, serializer_data: dict, request: Request) -> Response:
    """Shared body for FoodLogCreateView + InternalFoodLogView (DRF-247).

    Identical persistence path; only auth/actor differ. Returns the same
    response envelope so both client-app and bot consumers can deserialise
    with `FoodLogEntrySerializer`.
    """
    idempotency_key = request.META.get("HTTP_X_IDEMPOTENCY_KEY") or None
    try:
        log = FoodLogService().create(CreateFoodLogInput(
            user_id=user.id,
            portion_multiplier=serializer_data["portion_multiplier"],
            meal_type=serializer_data["meal_type"],
            scan_id=serializer_data.get("scan_id"),
            dish_name=serializer_data.get("dish_name"),
            logged_at=serializer_data.get("logged_at"),
            idempotency_key=idempotency_key,
        ))
    except InvalidInputError as exc:
        return error_response("VALIDATION_ERROR", str(exc))
    except ScanNotOwnedError:
        return error_response(
            "SCAN_NOT_FOUND",
            "Сканирование не найдено",
            status_code=status.HTTP_404_NOT_FOUND,
        )
    except DishNotRecognizedError as exc:
        logger.info(
            "nutrition.food_log.not_recognized user=%s err=%s", user.id, exc,
        )
        return error_response(
            "FOOD_NOT_RECOGNIZED",
            "Не удалось определить макросы блюда",
        )
    return success_response(
        FoodLogEntrySerializer(log).data,
        status_code=status.HTTP_201_CREATED,
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
        return _create_food_log_for(request.user, serializer.validated_data, request)


class InternalFoodLogView(APIView):
    """POST /api/v1/nutrition/internal/food-log/ — service-to-service log.

    DRF-247. Mirrors `FoodLogCreateView` but authenticates via service token
    + resolves actor from `X-External-User-ID`. Used by the MAX bot when a
    user clicks «Записать в дневник» in a scan card.
    """

    permission_classes = [IsServiceAccount]
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

        serializer = FoodLogCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )
        return _create_food_log_for(user, serializer.validated_data, request)


class InternalSummaryView(APIView):
    """GET /api/v1/nutrition/internal/summary/ — daily or progressive.

    DRF-247: ?date=YYYY-MM-DD → single-day NutritionSummaryResponseSerializer.
    DRF-266: ?period=7|14|28 → multi-week ProgressiveSummaryResponseSerializer.

    The two query params are mutually exclusive at the response level —
    presence of ``period`` switches the entire shape. Bot uses date= for
    `/дневник`, period= for the weekly progress unlock UX.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def get(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )

        q = NutritionSummaryQuerySerializer(data=request.query_params)
        if not q.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидный параметр date / period",
                details=q.errors,
            )

        # DRF-266 — progressive shape on ?period=.
        period = q.validated_data.get("period")
        if period is not None:
            from nutrition.serializers import (
                ProgressiveSummaryResponseSerializer,
            )
            progressive = NutritionSummaryService().progressive(
                user=user, period=period,
            )
            return success_response(
                ProgressiveSummaryResponseSerializer(progressive).data,
                status_code=status.HTTP_200_OK,
            )

        # Legacy single-day shape.
        day = q.validated_data.get("date") or datetime.now(dt_tz.utc).date()
        with_comment = bool(q.validated_data.get("with_comment", False))
        summary = NutritionSummaryService().summary(
            user_id=user.id, day=day, with_comment=with_comment,
        )
        return success_response(
            NutritionSummaryResponseSerializer(summary).data,
            status_code=status.HTTP_200_OK,
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
        with_comment = bool(q.validated_data.get("with_comment", False))

        summary = NutritionSummaryService().summary(
            user_id=request.user.id, day=day, with_comment=with_comment,
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


class InternalDeficitsView(APIView):
    """GET /api/v1/nutrition/internal/deficits/?days=7 — cross-domain bridge (DRF-248).

    Service-to-service. Returns aggregated deficit signals + an optional
    soft hint string the bot's AIConcierge feeds into ``render_system_prompt``
    via the ``extra_hint`` kwarg. Empty hint = nothing fired (caller still
    gets 200 so it can deterministically decide).
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def get(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )

        try:
            days = int(request.query_params.get("days", "7"))
        except (TypeError, ValueError):
            return error_response(
                "VALIDATION_ERROR",
                "Параметр days должен быть целым числом 1..14",
            )
        if days < 1 or days > 14:
            return error_response(
                "VALIDATION_ERROR",
                "Параметр days должен быть в диапазоне 1..14",
            )

        deficits = NutritionSummaryService().weekly_deficits(
            user_id=user.id, days=days,
        )
        hint_result = build_deficit_hint(deficits)

        return success_response(
            {
                "days_observed": deficits.days_observed,
                "protein_avg_pct_goal": deficits.protein_avg_pct_goal,
                "protein_low_streak_days": deficits.protein_low_streak_days,
                "hint": hint_result.hint,
                "fired_keys": hint_result.fired_keys,
            },
            status_code=status.HTTP_200_OK,
        )


class InternalReturningSuccessView(APIView):
    """GET /api/v1/nutrition/internal/insights/returning_success/ (DRF-305).

    Service-to-service. Detects users who fell off (3+ consecutive days
    <60% of kcal goal) and have come back (2+ consecutive days in
    [80, 110]% of goal, ending today). The bot's nudge engine fires the
    ``returning_success`` message off this signal — never reinforced
    with numbers; we surface only the boolean + the streak counts the
    bot needs for templating.

    Eating-disorder users always receive ``{detected: false}`` (spec
    §3.2 + §10) — numeric improvement is not framed as success here.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def get(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        insight = detect_returning_success(user_id=user.id)
        return success_response(
            ReturningSuccessResponseSerializer(insight).data,
            status_code=status.HTTP_200_OK,
        )


class InternalPatternsView(APIView):
    """GET /api/v1/nutrition/internal/patterns/ — behavioural patterns (DRF-304).

    Service-to-service. Returns the seven detector outputs with severity
    + display_hint. Cache: 12h per-user (spec §13) — Phase 3.3 nudges
    fire at most once per cycle, so staler-but-cheaper is fine.

    Health-flag suppression is applied inside the service: ED users
    don't see frequent_alcohol / low_protein / meal_skips; pregnant
    users don't see frequent_alcohol (clinical, not nudge).
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def get(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        result = detect_patterns(user_id=user.id)
        return success_response(
            PatternDetectionResponseSerializer(result).data,
            status_code=status.HTTP_200_OK,
        )


class InternalProfileView(APIView):
    """GET / POST /api/v1/nutrition/internal/profile/ (DRF-300).

    GET returns the full profile envelope or ``{exists: false}`` when no
    profile exists yet — the bot prefers a single status code over
    branching on 404.

    POST is PATCH-semantic: only fields present in the body mutate.
    Idempotency-Key (24-hour cache) makes POST safe to retry — the
    cached response is returned verbatim and no additional patch runs.

    Override audit (eating_disorder / pregnancy / breastfeeding / BMR
    floor ladder) is computed in nutrition_profile_service.compute_norms
    and surfaced in ``overrides_applied`` so the bot can render the
    «учла важное» panel.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def _resolve(self, request):
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return None, error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            ), external_user_id
        return user, None, external_user_id

    def get(self, request: Request) -> Response:
        user, err, external_user_id = self._resolve(request)
        if err is not None:
            return err
        return success_response(
            get_profile_response(user, external_user_id),
            status_code=status.HTTP_200_OK,
        )

    def post(self, request: Request) -> Response:
        user, err, external_user_id = self._resolve(request)
        if err is not None:
            return err

        serializer = NutritionProfileUpsertSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )

        idem = request.META.get("HTTP_IDEMPOTENCY_KEY") or None
        response = upsert_profile(
            user=user,
            external_user_id=external_user_id,
            payload=serializer.validated_data,
            idempotency_key=idem,
        )
        return success_response(response, status_code=status.HTTP_200_OK)


class InternalWaterCreateView(APIView):
    """POST /api/v1/nutrition/internal/water/ — log a beverage (DRF-302).

    Service-to-service. Request:
      - ``ml`` (10..3000)
      - ``beverage_slug`` (optional — null = pure water)
      - ``ts`` (optional — defaults to now())

    Idempotency: ``Idempotency-Key`` header is the canonical
    UUID5(user, ts, ml, slug). Replays return the original response.
    """

    permission_classes = [IsServiceAccount]
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

        serializer = WaterEntryCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )

        idem = request.META.get("HTTP_IDEMPOTENCY_KEY") or None
        try:
            resp = WaterEntryService().create(CreateWaterInput(
                user_id=user.id,
                ml=serializer.validated_data["ml"],
                beverage_slug=serializer.validated_data.get("beverage_slug") or None,
                ts=serializer.validated_data.get("ts"),
                idempotency_key=idem,
            ))
        except InvalidMlError as exc:
            return error_response("VALIDATION_ERROR", str(exc))
        except UnknownBeverageError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"Неизвестный напиток: {exc}",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        return success_response(
            WaterEntryResponseSerializer(resp).data,
            status_code=status.HTTP_201_CREATED,
        )


class InternalWaterDeleteView(APIView):
    """DELETE /api/v1/nutrition/internal/water/{entry_id}/ — soft-delete."""

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def delete(self, request: Request, pk: UUID) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )

        try:
            entry = WaterEntryService().soft_delete(user.id, pk)
        except EntryNotFoundError:
            return error_response(
                "NOT_FOUND",
                "Запись не найдена",
                status_code=status.HTTP_404_NOT_FOUND,
            )

        # Recompute today's running total without the deleted row so the
        # bot UI can update its progress without a follow-up GET.
        today_resp = WaterEntryService().today(user.id)
        return success_response(
            {
                "entry_id": str(entry.id),
                "deleted": True,
                "today_total_water_ml": today_resp.today_total_water_ml,
                "restore_window_expires_at": (
                    entry.deleted_at
                    + timedelta(minutes=RESTORE_WINDOW_MINUTES)
                ).isoformat(),
            },
            status_code=status.HTTP_200_OK,
        )


class InternalWaterRestoreView(APIView):
    """POST /api/v1/nutrition/internal/water/{entry_id}/restore/."""

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def post(self, request: Request, pk: UUID) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )

        try:
            entry = WaterEntryService().restore(user.id, pk)
        except EntryNotFoundError:
            return error_response(
                "NOT_FOUND",
                "Запись не найдена",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        except RestoreWindowExpiredError:
            return error_response(
                "RESTORE_WINDOW_EXPIRED",
                "Окно для восстановления истекло (15 минут)",
                status_code=status.HTTP_410_GONE,
            )

        today_resp = WaterEntryService().today(user.id)
        return success_response(
            {
                "entry_id": str(entry.id),
                "restored": True,
                "today_total_water_ml": today_resp.today_total_water_ml,
            },
            status_code=status.HTTP_200_OK,
        )


class InternalWaterTodayView(APIView):
    """GET /api/v1/nutrition/internal/water/today/ (spec §2.4)."""

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def get(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )

        resp = WaterEntryService().today(user.id)
        return success_response(
            WaterTodayResponseSerializerV3(resp).data,
            status_code=status.HTTP_200_OK,
        )


class InternalBeveragesView(APIView):
    """GET /api/v1/nutrition/internal/beverages/ — beverage catalog (DRF-301).

    Service-to-service. Returns the active catalog so the MAX bot can
    do alias-based free-text matching ("выпила кофе" → kofe_chernyi)
    and render UI labels («+200 мл (чашка)»).

    Cache-Control: max-age=3600 — content changes ≤ once a day, the
    bot is free to cache for an hour. No vary on user; the catalog is
    tenant-agnostic.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def get(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )

        qs = Beverage.objects.filter(is_active=True).order_by("category", "name_ru")
        resp = success_response(
            {"beverages": BeverageCatalogItemSerializer(qs, many=True).data},
            status_code=status.HTTP_200_OK,
        )
        resp["Cache-Control"] = "max-age=3600"
        return resp


# ---------------------------------------------------------------------------
# DRF-267 — /internal/insights/cross_domain/ views
# ---------------------------------------------------------------------------


def _cd_resolve_user(request: Request):
    """Resolve external_user_id → User. Shared across cross_domain views."""
    external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
    return resolve_external_user(external_user_id)


class InternalCrossDomainView(APIView):
    """GET /api/v1/nutrition/internal/insights/cross_domain/ (DRF-267).

    Returns top-1 cross-domain recommendation for the user or
    ``{has_insight: false}`` when no rule fires. Engine writes a
    CrossDomainShownRule row inline (PO-approved 2026-05-05).
    Auto-confirm-5min covers surfaces that never POST /seen/.

    LB-10 (DRF-269): wire format aligned to maxbot
    ``NutritionClient.get_cross_domain_insights`` parser — nested
    ``{has_insight, insight: {...}}`` envelope, ``rule_slug`` alias
    for ``rule_id``. The bot is already deployed with this parser
    shape across other B-* tickets.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def get(self, request: Request) -> Response:
        try:
            user = _cd_resolve_user(request)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        from nutrition.services.cross_domain_engine import CrossDomainEngine
        rec = CrossDomainEngine().evaluate(user=user, surface="bot")
        if rec is None:
            return success_response(
                {"has_insight": False},
                status_code=status.HTTP_200_OK,
            )
        return success_response(
            {
                "has_insight": True,
                "insight": {
                    "shown_id": rec.shown_id,
                    "rule_slug": rec.rule_id,
                    "nutrition_trigger": rec.nutrition_trigger,
                    "service_category_slug": rec.service_category_slug,
                    "insight_text": rec.insight_text,
                    "rationale_text": rec.rationale_text,
                    "disclaimer_text": rec.disclaimer_text,
                    "data_points": rec.data_points,
                    "severity": rec.severity,
                },
            },
            status_code=status.HTTP_200_OK,
        )


class InternalCrossDomainSeenView(APIView):
    """POST /api/v1/nutrition/internal/insights/cross_domain/{id}/seen/.

    Idempotent: first call sets seen_at = now(); subsequent calls leave
    it unchanged. Surface-side bookkeeping that activates cooldown
    immediately (overrides 5-min auto-confirm).
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def post(self, request: Request, shown_id) -> Response:
        try:
            user = _cd_resolve_user(request)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        from nutrition.models import CrossDomainShownRule
        try:
            shown = CrossDomainShownRule.objects.get(
                id=shown_id, user=user,
            )
        except CrossDomainShownRule.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Cross-domain row not found",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        if shown.seen_at is None:
            shown.seen_at = timezone.now()
            shown.save(update_fields=["seen_at"])
        return success_response(
            {"shown_id": str(shown.id), "seen_at": shown.seen_at.isoformat()},
            status_code=status.HTTP_200_OK,
        )


class InternalCrossDomainDismissView(APIView):
    """POST /api/v1/nutrition/internal/insights/cross_domain/{id}/dismiss/.

    Body: ``{"action": "dismissed" | "paused_7d"}``. Sets
    ``user_action`` for cooldown engine to read (skip-extend +7 days
    for dismissed; double-skip → 60-day pause; paused_7d treated like
    dismissed but with shorter intent semantics).
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def post(self, request: Request, shown_id) -> Response:
        try:
            user = _cd_resolve_user(request)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        from nutrition.serializers import (
            CrossDomainDismissRequestSerializer,
        )
        ser = CrossDomainDismissRequestSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Invalid action — must be dismissed | paused_7d",
                details=ser.errors,
            )
        from nutrition.models import CrossDomainShownRule
        try:
            shown = CrossDomainShownRule.objects.get(
                id=shown_id, user=user,
            )
        except CrossDomainShownRule.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Cross-domain row not found",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        shown.user_action = ser.validated_data["action"]
        shown.save(update_fields=["user_action"])
        return success_response(
            {"shown_id": str(shown.id), "user_action": shown.user_action},
            status_code=status.HTTP_200_OK,
        )


class InternalCrossDomainConvertView(APIView):
    """POST /api/v1/nutrition/internal/insights/cross_domain/{id}/convert/.

    Body: ``{"appointment_id": uuid}``. Marks the row as converted and
    attaches the Appointment for revenue attribution. Validates the
    appointment belongs to the same user (prevents cross-user spoofing).
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def post(self, request: Request, shown_id) -> Response:
        try:
            user = _cd_resolve_user(request)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        from nutrition.serializers import (
            CrossDomainConvertRequestSerializer,
        )
        ser = CrossDomainConvertRequestSerializer(data=request.data)
        if not ser.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "appointment_id required",
                details=ser.errors,
            )
        from nutrition.models import CrossDomainShownRule
        try:
            shown = CrossDomainShownRule.objects.get(
                id=shown_id, user=user,
            )
        except CrossDomainShownRule.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Cross-domain row not found",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        from appointments.models import Appointment
        try:
            appt = Appointment.objects.get(
                id=ser.validated_data["appointment_id"],
                client=user,
            )
        except Appointment.DoesNotExist:
            return error_response(
                "NOT_FOUND", "Appointment not found for this user",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        shown.user_action = "converted"
        shown.appointment = appt
        shown.save(update_fields=["user_action", "appointment"])
        return success_response(
            {
                "shown_id": str(shown.id),
                "appointment_id": str(appt.id),
                "user_action": "converted",
            },
            status_code=status.HTTP_200_OK,
        )


class InternalCrossDomainHistoryView(APIView):
    """GET /api/v1/nutrition/internal/insights/cross_domain/history/?limit=20.

    Transparency UI source. Returns user's recent CrossDomainShownRule
    rows newest-first. Default limit 20, max 100.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    def get(self, request: Request) -> Response:
        try:
            user = _cd_resolve_user(request)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        try:
            limit = int(request.query_params.get("limit", "20"))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 100))

        from nutrition.models import CrossDomainShownRule
        from nutrition.serializers import (
            CrossDomainHistoryEntrySerializer,
        )
        rows = (
            CrossDomainShownRule.objects
            .filter(user=user)
            .select_related("rule")
            .order_by("-shown_at")[:limit]
        )
        return success_response(
            {
                "history": CrossDomainHistoryEntrySerializer(
                    rows, many=True,
                ).data,
            },
            status_code=status.HTTP_200_OK,
        )
