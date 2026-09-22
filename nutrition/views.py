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
from drf_spectacular.utils import OpenApiResponse, extend_schema, inline_serializer

from core.deprecation import DeprecatedAliasMixin
from rest_framework import permissions, serializers as drf_serializers, status
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
    ManualTargetsSerializer,
    BeverageCatalogItemSerializer,
    CrossDomainConvertRequestSerializer,
    CrossDomainDismissRequestSerializer,
    CrossDomainHistoryResponseSerializer,
    FoodLogCreateSerializer,
    FoodLogEntrySerializer,
    FoodLogUpdateSerializer,
    FoodEstimateRequestSerializer,
    FoodScanResponseSerializer,
    NutritionProfileResponseSerializer,
    NutritionProfileUpsertSerializer,
    NutritionSummaryQuerySerializer,
    NutritionSummaryResponseSerializer,
    PatternDetectionResponseSerializer,
    ReturningSuccessResponseSerializer,
    SavedMealCreateSerializer,
    SavedMealSerializer,
    ScanRequestSerializer,
    WaterEntryCreateSerializer,
    WaterEntryResponseSerializer,
    WaterLogCreateSerializer,
    WaterLogResponseSerializer,
    WaterTodayResponseSerializer,
    WaterTodayResponseSerializerV3,
)
from nutrition.services.personal_calculation_consent import (
    PERSONAL_CALCULATION,
    PersonalCalculationConsentRequired,
    require_consent,
)
from nutrition.services.pattern_detection_service import detect_patterns
from nutrition.services.returning_success_service import detect_returning_success
from nutrition.services.manual_targets_service import (
    CaloriesBelowFloor,
    ConfirmationRequired,
    NothingToSet,
    set_manual_targets,
)
from nutrition.services.profile_upsert_service import (
    LegacyDefaultUnconfirmed,
    NothingToConfirm,
    confirm_targets,
    get_profile_response,
    serialize_profile,
    upsert_profile,
)
from nutrition.services.food_log_service import (
    CreateFoodLogInput,
    DishNotRecognizedError,
    FoodLogService,
    InvalidInputError,
    MANUAL_DISH_BASELINE_G,
    ScanNotOwnedError,
)
from nutrition.services import food_scan_budget
from nutrition.services.food_scanner_router import (
    AllProvidersFailedError,
    FoodScannerRouter,
)
from nutrition.services.deficit_hints import build_deficit_hint
from nutrition.services.nutrition_lookup_factory import build_nutrition_lookup
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


def _budget_refusal(user) -> Response | None:
    """DRF-2145: занять попытку распознавания или отказать по имени.

    429 ``FOOD_SCAN_DAILY_LIMIT`` — личный потолок, ``retry_after`` до полуночи
    UTC; 503 ``FOOD_SCAN_BUDGET_EXHAUSTED`` — общий. Тексты для человека
    говорит бот / Mini App по коду; здесь — код и числа. Лог — без
    идентификатора человека.
    """
    try:
        food_scan_budget.reserve(user)
    except food_scan_budget.DailyLimitExceeded as exc:
        logger.info("nutrition.scan.budget_refused code=%s used=%s", exc.code, exc.used)
        return error_response(
            exc.code,
            "Дневной лимит распознавания фото исчерпан",
            details=exc.details,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )
    except food_scan_budget.BudgetExhausted as exc:
        logger.info("nutrition.scan.budget_refused code=%s used=%s", exc.code, exc.used)
        return error_response(
            exc.code,
            "Дневной бюджет распознавания фото исчерпан",
            details=exc.details,
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return None


def _record_provider_cost(scan: FoodScan, result) -> None:
    """DRF-2145: токены как пришли, стоимость — по ценам настроек или null."""
    usage = dict(getattr(result, "usage", None) or {})
    _record_usage(scan, usage)


def _record_usage(scan: FoodScan, usage: dict) -> None:
    scan.provider_usage = usage
    scan.provider_cost_usd = food_scan_budget.cost_usd(usage)
    food_scan_budget.record_cost(scan.provider_cost_usd)


def _unavailable_details(exc: AllProvidersFailedError) -> dict | None:
    """DRF-2318: стойкий отказ распознавателя — ``details`` для 503.

    Код ответа прежний (``FOOD_API_UNAVAILABLE``), чтобы прежние читатели не
    сломались; бот по ``permanent`` не обещает «через минуту». Причина —
    закрытое слово, текста провайдера в ответе нет.
    """
    reason = exc.permanent_reason
    return {"permanent": True, "reason": reason} if reason else None


def _settle_not_recognized(scan: FoodScan, exc: AllProvidersFailedError, user) -> None:
    """DRF-2218, §63: «не еда» — вернуть ЛИЧНУЮ попытку дня; вызов(ы)
    провайдера оплачены — их стоимость записывается (``partial`` каждого
    провайдера, ответившего «низкая уверенность»), общий потолок не
    возвращается. Зовётся только при ``is_low_confidence_only``.
    """
    summed: dict[str, int] = {}
    for err in (exc.primary_err, exc.fallback_err):
        usage = getattr(getattr(err, "partial", None), "usage", None) or {}
        for name, value in usage.items():
            if isinstance(value, int) and not isinstance(value, bool):
                summed[name] = summed.get(name, 0) + value
    _record_usage(scan, summed)
    food_scan_budget.refund_personal(user)


def _settle_permanent_refusal(exc: AllProvidersFailedError, user) -> None:
    """DRF-2322: стойкий отказ распознавателя — вернуть попытку дня.

    Поломка наша (счёт не активен, ключ отвергнут, квота исчерпана), и день
    человека за неё не тратится. Личная попытка возвращается всегда; общий
    потолок — по факту оплаченных вызовов: ``permanent_reason`` выставляется,
    только когда ВСЕ опрошенные провайдеры отказали стойко, а такой отказ не
    несёт ``partial``, то есть вызова, за который заплачено, не было. Если
    частичный результат всё же пришёл, потолок остаётся потраченным (DRF-2218).
    """
    food_scan_budget.refund_personal(user)
    paid = any(
        getattr(getattr(err, "partial", None), "usage", None)
        for err in (exc.primary_err, exc.fallback_err)
    )
    if not paid:
        food_scan_budget.refund_total()


def _settle_nutrition(scan, facts) -> None:
    """DRF-2335: записать питание в строку и назвать в логе, если его нет.

    До этого листа пустое питание было молчаливым: человек не получал калорий,
    а узнать почему можно было только выборкой по ``raw_response``. Теперь
    причина названа словом:

    * ``dish_not_found`` — справочник промахнулся, чисел у нас нет;
    * ``portion_unknown`` — блюдо нашли, но провайдер не назвал порцию, и
      итоги не посчитаны (числа на 100 г при этом есть и уходят человеку).

    Отказ распознавателя отдельной строкой здесь не пишется — до этого места
    он не доходит: его называет ``all_providers_failed`` выше по ветке.

    В строку лога не попадает ни название блюда, ни ингредиенты, ни человек:
    журнал общий. Идёт ``scan`` (UUID строки) — по нему находят запись, не
    называя того, кто прислал фото.
    """
    scan.nutrition = facts.to_dict() if facts is not None else None

    reason = _nutrition_gap_reason(facts)
    if reason is None:
        return
    logger.info(
        "nutrition.scan.no_nutrition scan=%s reason=%s provider=%s",
        scan.id, reason, scan.provider_used,
    )


def _nutrition_gap_reason(facts) -> str | None:
    """Почему у скана нет итогов питания, или ``None``, если они есть.

    Единственное место, где этот вопрос решается. П. 3 DRF-2335 (признак
    наружу, в ответ) ждёт слова владельца — когда оно будет, брать причину
    нужно отсюда, а не считать её заново у сериализатора.
    """
    if facts is None:
        return "dish_not_found"
    if facts.kcal is None:
        return "portion_unknown"
    return None


class FoodScanView(APIView):
    """POST /api/v1/nutrition/scan/."""

    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    parser_classes = [MultiPartParser, FormParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan"
    serializer_class = ScanRequestSerializer

    @extend_schema(
        request=ScanRequestSerializer,
        responses={
            200: FoodScanResponseSerializer,
            400: OpenApiResponse(description="Validation error or food not recognised"),
            503: OpenApiResponse(description="All food-scan providers failed"),
        },
    )
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

        # DRF-2145: бюджет — до строки и до провайдера; попытка считается.
        refusal = _budget_refusal(request.user)
        if refusal is not None:
            return refusal

        scan = FoodScan(user=request.user)
        scan.image.save(
            f"{scan.id}.jpg",
            ContentFile(image_bytes),
            save=False,
        )
        # 152-ФЗ ст. 18 п. 5 — localize-first. The DB row tying user
        # to image MUST exist in an RF-located DB BEFORE the photo
        # bytes leave Russia (OpenAI Vision call below). The file
        # itself is already in MinIO at this point via image.save(
        # save=False) — the missing piece was the structured record.
        # Without this commit, a crash during the router.scan() call
        # below would leave an orphan file with no DB linkage, AND a
        # strict reading of ст. 18 п. 5 would flag the cross-border
        # transfer as occurring before the RF-side record exists.
        scan.save()

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
                _settle_not_recognized(scan, exc, request.user)
            else:
                error_code = "FOOD_API_UNAVAILABLE"
                http_status = status.HTTP_503_SERVICE_UNAVAILABLE
                msg = "Сервис распознавания временно недоступен"
                if exc.permanent_reason:
                    _settle_permanent_refusal(exc, request.user)

            scan.error_code = error_code
            scan.error_message = str(exc)[:500]
            scan.save()
            logger.warning(
                "nutrition.scan.all_providers_failed user=%s code=%s err=%s",
                request.user.id, error_code, exc,
            )
            return error_response(
                error_code, msg, status_code=http_status,
                details=None if exc.is_low_confidence_only else _unavailable_details(exc),
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
        _record_provider_cost(scan, outcome.result)
        scan.raw_response = outcome.result.raw_response

        # Slice 3a: seed-only lookup. Misses leave nutrition=null and the
        # mobile client shows "уточните порцию вручную". OFF/USDA HTTP
        # fallback ships in 3a'.
        facts = build_nutrition_lookup().lookup(
            outcome.result.dish_name,
            ingredients=outcome.result.ingredients,
            portion_g=outcome.result.portion_g,
        )
        _settle_nutrition(scan, facts)

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
    serializer_class = ScanRequestSerializer

    @extend_schema(
        tags=["internal"],
        request=ScanRequestSerializer,
        responses={
            200: FoodScanResponseSerializer,
            400: OpenApiResponse(description="Validation error or food not recognised"),
            503: OpenApiResponse(description="All food-scan providers failed"),
        },
    )
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

        # DRF-2145: бюджет — до строки и до провайдера; попытка считается.
        refusal = _budget_refusal(user)
        if refusal is not None:
            return refusal

        scan = FoodScan(user=user)
        scan.image.save(
            f"{scan.id}.jpg",
            ContentFile(image_bytes),
            save=False,
        )
        # 152-ФЗ ст. 18 п. 5 — see FoodScanView above. Same rationale
        # for the internal (bot-driven) path: the structured FoodScan
        # row MUST be committed to the RF DB before the photo leaves
        # the country via OpenAI Vision. resolve_external_user above
        # has already either fetched or created the proxy User in the
        # RF DB; this completes the localize-first record.
        scan.save()

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
                _settle_not_recognized(scan, exc, user)
            else:
                error_code = "FOOD_API_UNAVAILABLE"
                http_status = status.HTTP_503_SERVICE_UNAVAILABLE
                msg = "Сервис распознавания временно недоступен"
                if exc.permanent_reason:
                    _settle_permanent_refusal(exc, user)

            scan.error_code = error_code
            scan.error_message = str(exc)[:500]
            scan.save()
            logger.warning(
                "nutrition.internal_scan.all_providers_failed user=%s code=%s err=%s",
                user.id, error_code, exc,
            )
            return error_response(
                error_code, msg, status_code=http_status,
                details=None if exc.is_low_confidence_only else _unavailable_details(exc),
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
        _record_provider_cost(scan, outcome.result)
        scan.raw_response = outcome.result.raw_response

        facts = build_nutrition_lookup().lookup(
            outcome.result.dish_name,
            ingredients=outcome.result.ingredients,
            portion_g=outcome.result.portion_g,
        )
        _settle_nutrition(scan, facts)

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
            entry_origin=serializer_data.get("entry_origin"),
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
    serializer_class = FoodLogCreateSerializer

    @extend_schema(
        request=FoodLogCreateSerializer,
        responses={
            201: FoodLogEntrySerializer,
            400: OpenApiResponse(description="Validation or dish-not-recognised"),
            404: OpenApiResponse(description="Scan not owned by caller"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = FoodLogCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )
        return _create_food_log_for(request.user, serializer.validated_data, request)


class InternalFoodEstimateView(APIView):
    """POST /api/v1/nutrition/internal/food-estimate/ — оценка блюда без записи.

    DRF-1837, §109 шаги 2–3: «Ayla распознаёт блюдо и оценивает состав и
    порцию → показывает экран „Я распознала так“», а в дневник — только
    после подтверждения (шаг 6). Ручная запись по ``dish_name`` уже была
    (``internal/food-log/``), но она ПИШЕТ сразу; предъявить оценку до
    записи было нечем. Эта ручка — та же ``NutritionLookup``, что у
    ручной записи, на те же граммы, и ни одной строки в базе: ни
    ``FoodLog``, ни ``FoodScan``, ни прокси-пользователя (актор здесь не
    нужен — число не принадлежит никому, пока его не подтвердили).

    Ответ несёт ``portion_estimated``: ``true``, когда граммов человек не
    называл и оценка взята на базовые 100 г, — бот обязан показать это
    словом «примерно»/«оценка» (§109 шаг 4).
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"
    serializer_class = FoodEstimateRequestSerializer

    @extend_schema(
        tags=["internal"],
        request=FoodEstimateRequestSerializer,
        responses={
            200: OpenApiResponse(description="Оценка блюда (без записи)"),
            400: OpenApiResponse(description="Validation error or food not recognised"),
        },
    )
    def post(self, request: Request) -> Response:
        serializer = FoodEstimateRequestSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR",
                "Невалидные данные",
                details=serializer.errors,
            )
        dish_name = serializer.validated_data["dish_name"]
        named_portion = serializer.validated_data.get("portion_g")
        portion_g = named_portion if named_portion is not None else MANUAL_DISH_BASELINE_G
        facts = build_nutrition_lookup().lookup(dish_name, portion_g=portion_g)
        if facts is None or facts.kcal is None:
            return error_response(
                "FOOD_NOT_RECOGNIZED",
                "Не удалось определить макросы блюда",
            )
        return success_response(
            {
                "matched_dish": facts.matched_dish,
                "source": facts.source,
                "portion_g": portion_g,
                "portion_estimated": named_portion is None,
                "kcal": facts.kcal,
                "protein_g": facts.protein_g,
                "fat_g": facts.fat_g,
                "carbs_g": facts.carbs_g,
                "kcal_per_100g": facts.kcal_per_100g,
            },
            status_code=status.HTTP_200_OK,
        )


class InternalFoodLogView(APIView):
    """POST /api/v1/nutrition/internal/food-log/ — service-to-service log.

    DRF-247. Mirrors `FoodLogCreateView` but authenticates via service token
    + resolves actor from `X-External-User-ID`. Used by the MAX bot when a
    user clicks «Записать в дневник» in a scan card.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"
    serializer_class = FoodLogCreateSerializer

    @extend_schema(
        tags=["internal"],
        request=FoodLogCreateSerializer,
        responses={
            201: FoodLogEntrySerializer,
            400: OpenApiResponse(description="Validation or dish-not-recognised"),
            404: OpenApiResponse(description="Scan not owned by caller"),
        },
    )
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


def _food_log_actor(request: Request):
    external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
    try:
        return resolve_external_user(external_user_id), None
    except InvalidExternalUserIDError as exc:
        return None, error_response(
            "VALIDATION_ERROR",
            f"X-External-User-ID невалиден: {exc}",
        )


def _food_log_refusal(exc: Exception) -> Response:
    from nutrition.services.food_log_edit_service import (
        FoodLogManagedByWaterError,
        FoodLogNotFoundError,
        RESTORE_WINDOW_MINUTES,
        RestoreWindowExpiredError,
    )

    if isinstance(exc, FoodLogNotFoundError):
        return error_response(
            "NOT_FOUND", "Запись не найдена", status_code=status.HTTP_404_NOT_FOUND,
        )
    if isinstance(exc, FoodLogManagedByWaterError):
        return error_response(
            "CONFLICT",
            "Эту запись ведёт учёт воды — её убирает отмена стакана",
            status_code=status.HTTP_409_CONFLICT,
        )
    if isinstance(exc, RestoreWindowExpiredError):
        return error_response(
            "RESTORE_WINDOW_EXPIRED",
            f"Окно для восстановления истекло ({RESTORE_WINDOW_MINUTES} минут)",
            status_code=status.HTTP_410_GONE,
        )
    return error_response(
        "CONFLICT", "Запись нельзя пересчитать", status_code=status.HTTP_409_CONFLICT,
    )


class InternalFoodLogDetailView(APIView):
    """PATCH / DELETE /api/v1/nutrition/internal/food-log/{entry_id}/ — DRF-1838.

    §109 шаг 7: сохранённую запись можно исправить или удалить. Владелец —
    только сам человек из ``X-External-User-ID``; чужая запись — 404.
    Правила пересчёта и окна — ``nutrition.services.food_log_edit_service``.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"
    serializer_class = FoodLogUpdateSerializer

    @extend_schema(
        tags=["internal"],
        request=FoodLogUpdateSerializer,
        responses={
            200: FoodLogEntrySerializer,
            400: OpenApiResponse(description="Nothing to change / invalid values"),
            404: OpenApiResponse(description="Entry not found for this person"),
            409: OpenApiResponse(description="Entry mirrors a water entry"),
        },
    )
    def patch(self, request: Request, pk: UUID) -> Response:
        from nutrition.services.food_log_edit_service import FoodLogEditError, update_food_log

        user, refusal = _food_log_actor(request)
        if refusal is not None:
            return refusal
        serializer = FoodLogUpdateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Невалидные данные", details=serializer.errors,
            )
        try:
            log = update_food_log(user_id=user.id, log_id=pk, **serializer.validated_data)
        except FoodLogEditError as exc:
            return _food_log_refusal(exc)
        return success_response(FoodLogEntrySerializer(log).data)

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: inline_serializer(
                name="InternalFoodLogDeleteResponse",
                fields={
                    "entry_id": drf_serializers.UUIDField(),
                    "deleted": drf_serializers.BooleanField(),
                    "restore_window_expires_at": drf_serializers.DateTimeField(),
                },
            ),
            404: OpenApiResponse(description="Entry not found for this person"),
            409: OpenApiResponse(description="Entry mirrors a water entry"),
        },
    )
    def delete(self, request: Request, pk: UUID) -> Response:
        from nutrition.services.food_log_edit_service import (
            RESTORE_WINDOW_MINUTES,
            FoodLogEditError,
            delete_food_log,
        )

        user, refusal = _food_log_actor(request)
        if refusal is not None:
            return refusal
        try:
            deleted = delete_food_log(user_id=user.id, log_id=pk)
        except FoodLogEditError as exc:
            return _food_log_refusal(exc)
        return success_response(
            {
                "entry_id": str(deleted.id),
                "deleted": True,
                "restore_window_expires_at": (
                    deleted.deleted_at + timedelta(minutes=RESTORE_WINDOW_MINUTES)
                ).isoformat(),
            },
        )


class InternalFoodLogRestoreView(APIView):
    """POST /api/v1/nutrition/internal/food-log/{entry_id}/restore/ — DRF-1838."""

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: FoodLogEntrySerializer,
            404: OpenApiResponse(description="No deletion of this entry for this person"),
            410: OpenApiResponse(description="Restore window expired"),
        },
    )
    def post(self, request: Request, pk: UUID) -> Response:
        from nutrition.services.food_log_edit_service import FoodLogEditError, restore_food_log

        user, refusal = _food_log_actor(request)
        if refusal is not None:
            return refusal
        try:
            log = restore_food_log(user_id=user.id, log_id=pk)
        except FoodLogEditError as exc:
            return _food_log_refusal(exc)
        return success_response(FoodLogEntrySerializer(log).data)


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
    serializer_class = NutritionSummaryResponseSerializer

    @extend_schema(
        tags=["internal"],
        parameters=[NutritionSummaryQuerySerializer],
        responses={
            200: NutritionSummaryResponseSerializer,
            400: OpenApiResponse(description="Invalid date / period"),
        },
    )
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
    serializer_class = NutritionSummaryResponseSerializer

    @extend_schema(
        parameters=[NutritionSummaryQuerySerializer],
        responses={
            200: NutritionSummaryResponseSerializer,
            400: OpenApiResponse(description="Invalid date param"),
        },
    )
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


#: DRF-2269 — публичные маршруты кнопочного ``WaterLog`` устарели: бот и Mini App
#: пишут в ``WaterEntry`` через ``internal/water/*``, других вызывающих в видимых
#: репозиториях нет; мобильный клиент BeautyGO не виден — поэтому маршруты не
#: сняты, а помечены (заголовки, строка лога на вызов, OpenAPI ``deprecated``).
#: Снятие — отдельным листом после недели нулевых вызовов на стенде. Дата —
#: предложение, её назначает владелец.
WATERLOG_SUNSET = "Sat, 31 Oct 2026 23:59:59 GMT"


class WaterLogCreateView(DeprecatedAliasMixin, APIView):
    """POST /api/v1/nutrition/water/ — log a glass of water. УСТАРЕЛ (DRF-2269)."""

    deprecated_path = "/api/v1/nutrition/water/"
    sunset_date = WATERLOG_SUNSET
    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "water"
    serializer_class = WaterLogCreateSerializer

    @extend_schema(
        deprecated=True,
        request=WaterLogCreateSerializer,
        responses={
            200: WaterLogResponseSerializer,
            400: OpenApiResponse(description="Validation error"),
        },
    )
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


class WaterLogDeleteView(DeprecatedAliasMixin, APIView):
    """DELETE /api/v1/nutrition/water/{id}/ — undo a glass. УСТАРЕЛ (DRF-2269).

    Returns the same WaterLogResponse shape so the mobile UI can
    update its progress ring without a follow-up GET.
    """

    deprecated_path = "/api/v1/nutrition/water/<id>/"
    sunset_date = WATERLOG_SUNSET
    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "water"
    serializer_class = WaterLogResponseSerializer

    @extend_schema(
        deprecated=True,
        request=None,
        responses={
            200: WaterLogResponseSerializer,
            404: OpenApiResponse(description="WaterLog not found for caller"),
        },
    )
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


class WaterTodayView(DeprecatedAliasMixin, APIView):
    """GET /api/v1/nutrition/water/today/ — list today's glasses. УСТАРЕЛ (DRF-2269)."""

    deprecated_path = "/api/v1/nutrition/water/today/"
    sunset_date = WATERLOG_SUNSET
    permission_classes = [permissions.IsAuthenticated, IsClientApp, IsClient]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "water"
    serializer_class = WaterTodayResponseSerializer

    @extend_schema(deprecated=True, responses={200: WaterTodayResponseSerializer})
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

    @extend_schema(
        tags=["internal"],
        responses={
            200: inline_serializer(
                name="InternalDeficitsResponse",
                fields={
                    "days_observed": drf_serializers.IntegerField(),
                    "protein_avg_pct_goal": drf_serializers.FloatField(allow_null=True),
                    "protein_low_streak_days": drf_serializers.IntegerField(),
                    "hint": drf_serializers.CharField(allow_null=True, allow_blank=True),
                    "fired_keys": drf_serializers.ListField(
                        child=drf_serializers.CharField(),
                    ),
                },
            ),
            400: OpenApiResponse(description="Invalid days param (1..14)"),
        },
    )
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
    serializer_class = ReturningSuccessResponseSerializer

    @extend_schema(
        tags=["internal"],
        responses={200: ReturningSuccessResponseSerializer},
    )
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
    serializer_class = PatternDetectionResponseSerializer

    @extend_schema(
        tags=["internal"],
        responses={200: PatternDetectionResponseSerializer},
    )
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
    serializer_class = NutritionProfileResponseSerializer

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

    @extend_schema(
        tags=["internal"],
        responses={200: NutritionProfileResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        user, err, external_user_id = self._resolve(request)
        if err is not None:
            return err
        return success_response(
            get_profile_response(user, external_user_id),
            status_code=status.HTTP_200_OK,
        )

    @extend_schema(
        tags=["internal"],
        request=NutritionProfileUpsertSerializer,
        responses={
            200: NutritionProfileResponseSerializer,
            400: OpenApiResponse(description="Validation error"),
        },
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

        # §92, срез N-a2: параметры тела принимаются только с
        # утверждением о согласии. Сторож стоит ПОСЛЕ валидации и ДО
        # кэша идемпотентности — оба порядка намеренны.
        #
        # После валидации: отказ по согласию не должен подменяться
        # отказом по формату, иначе вызывающий чинит не то.
        #
        # До кэша: иначе первый запрос без утверждения, попавший в кэш
        # ДО этой правки, повторно отдавался бы как успешный — гейт
        # обходился бы собственной историей.
        try:
            require_consent(serializer.validated_data)
        except PersonalCalculationConsentRequired as exc:
            logger.info(
                "nutrition.profile.consent_refused user=%s fields=%s",
                user.pk,
                exc.fields,
            )
            return error_response(
                exc.code,
                str(exc),
                details={"fields": exc.fields, "consent_type": PERSONAL_CALCULATION},
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        idem = request.META.get("HTTP_IDEMPOTENCY_KEY") or None
        response = upsert_profile(
            user=user,
            external_user_id=external_user_id,
            payload=serializer.validated_data,
            idempotency_key=idem,
        )
        return success_response(response, status_code=status.HTTP_200_OK)


class InternalProfileTargetsConfirmView(APIView):
    """POST /api/v1/nutrition/internal/profile/targets/confirm/ (§5.1).

    Человек подтверждает предложенный ориентир: ``ayla_proposed`` →
    ``ayla_calculated``, ``targets_confirmed_at`` — момент подтверждения.
    Тела нет: подтверждается ровно то, что предложено, — число, которое
    человек видел. Отдаёт конверт профиля и ``confirmation.outcome``:
    ``confirmed`` либо ``already_confirmed`` (повтор кнопки — не ошибка
    и не новое событие).

    Подтверждать нечего (``none`` / ``unknown_legacy`` / ``user_entered``,
    профиля нет) — ``409 NOTHING_TO_CONFIRM`` с текущим источником в
    ``details``: вызывающему нужно отличить «ещё не считали» от
    «поставлено рукой», это разные ответы человеку.

    Кэша идемпотентности нет намеренно: повтор и так безопасен, а
    кэшированный ответ скрыл бы пересчёт, случившийся между нажатиями.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"
    serializer_class = NutritionProfileResponseSerializer

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: NutritionProfileResponseSerializer,
            409: OpenApiResponse(description="Подтверждать нечего"),
        },
    )
    def post(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        try:
            body, outcome = confirm_targets(user=user, external_user_id=external_user_id)
        except LegacyDefaultUnconfirmed as exc:
            logger.info(
                "nutrition.targets.confirm_refused_legacy user=%s fields=%s",
                user.pk,
                ",".join(exc.fields),
            )
            return error_response(
                exc.code,
                str(exc),
                details={"fields": exc.fields},
                status_code=status.HTTP_409_CONFLICT,
            )
        except NothingToConfirm as exc:
            logger.info(
                "nutrition.targets.confirm_refused user=%s source=%s",
                user.pk,
                exc.source,
            )
            return error_response(
                exc.code,
                str(exc),
                details={"targets_source": exc.source},
                status_code=status.HTTP_409_CONFLICT,
            )
        logger.info(
            "nutrition.targets.confirm user=%s outcome=%s",
            user.pk,
            outcome,
        )
        body = dict(body)
        body["confirmation"] = {"outcome": outcome}
        return success_response(body, status_code=status.HTTP_200_OK)


class InternalProfileTargetsManualView(APIView):
    """POST /api/v1/nutrition/internal/profile/targets/manual/ (§5.1).

    Человек задаёт норму сам — калории и/или воду. Единственный писатель
    источника ``user_entered``. Посчитанное (макросы, RDA, bmr) стирается:
    набор одного происхождения. Пороги §85 — в сервисе:

    * калории ``< 1000`` — ``422 CALORIES_BELOW_FLOOR``, не сохраняется;
    * калории ``1000–1199`` — сохраняется, ``warnings: ["calories_low"]``;
    * отклонение от поддержания ``> 30 %`` (поддержание — от снимка
      состоявшегося расчёта; нет снимка — ``deviation_check: "unavailable"``)
      — ``409 CONFIRMATION_REQUIRED`` (``kind: calories_deviation``) без
      ``confirm_deviation=true``;
    * вода вне ``1000–5000`` — ``409 CONFIRMATION_REQUIRED``
      (``kind: water_out_of_range``) без ``confirm_water_out_of_range=true``,
      с ним — сохраняется с предупреждением.

    Ответ: конверт профиля + ``manual_targets: {set, warnings, deviation}``.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"
    serializer_class = NutritionProfileResponseSerializer

    @extend_schema(
        tags=["internal"],
        request=ManualTargetsSerializer,
        responses={
            200: NutritionProfileResponseSerializer,
            400: OpenApiResponse(description="Validation error"),
            409: OpenApiResponse(description="Нужно подтверждение"),
            422: OpenApiResponse(description="Ниже порога — не сохраняется"),
        },
    )
    def post(self, request: Request) -> Response:
        external_user_id = request.META.get("HTTP_X_EXTERNAL_USER_ID", "")
        try:
            user = resolve_external_user(external_user_id)
        except InvalidExternalUserIDError as exc:
            return error_response(
                "VALIDATION_ERROR",
                f"X-External-User-ID невалиден: {exc}",
            )
        serializer = ManualTargetsSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Невалидные данные", details=serializer.errors,
            )
        data = serializer.validated_data
        try:
            profile, report = set_manual_targets(
                user=user,
                calories_kcal=data.get("calories_kcal"),
                water_ml=data.get("water_ml"),
                confirm_deviation=bool(data.get("confirm_deviation")),
                confirm_water_out_of_range=bool(data.get("confirm_water_out_of_range")),
            )
        except NothingToSet as exc:
            return error_response(exc.code, str(exc), details=exc.details)
        except CaloriesBelowFloor as exc:
            logger.info(
                "nutrition.targets.manual_refused user=%s code=%s",
                user.pk, exc.code,
            )
            return error_response(
                exc.code, str(exc), details=exc.details,
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except ConfirmationRequired as exc:
            logger.info(
                "nutrition.targets.manual_confirmation_required user=%s kind=%s",
                user.pk, exc.kind,
            )
            return error_response(
                exc.code, str(exc), details=exc.details,
                status_code=status.HTTP_409_CONFLICT,
            )
        logger.info(
            "nutrition.targets.manual user=%s set=%s warnings=%s",
            user.pk, report["set"], report["warnings"],
        )
        body = dict(serialize_profile(profile, external_user_id))
        body["manual_targets"] = report
        return success_response(body, status_code=status.HTTP_200_OK)


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
    serializer_class = WaterEntryCreateSerializer

    @extend_schema(
        tags=["internal"],
        request=WaterEntryCreateSerializer,
        responses={
            201: WaterEntryResponseSerializer,
            400: OpenApiResponse(description="Validation / unknown beverage"),
        },
    )
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

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: inline_serializer(
                name="InternalWaterDeleteResponse",
                fields={
                    "entry_id": drf_serializers.UUIDField(),
                    "deleted": drf_serializers.BooleanField(),
                    "today_total_water_ml": drf_serializers.IntegerField(),
                    "restore_window_expires_at": drf_serializers.DateTimeField(),
                },
            ),
            404: OpenApiResponse(description="Entry not found"),
        },
    )
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

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: inline_serializer(
                name="InternalWaterRestoreResponse",
                fields={
                    "entry_id": drf_serializers.UUIDField(),
                    "restored": drf_serializers.BooleanField(),
                    "today_total_water_ml": drf_serializers.IntegerField(),
                },
            ),
            404: OpenApiResponse(description="Entry not found"),
            410: OpenApiResponse(description="Restore window expired"),
        },
    )
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
    serializer_class = WaterTodayResponseSerializerV3

    @extend_schema(
        tags=["internal"],
        responses={200: WaterTodayResponseSerializerV3},
    )
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
    serializer_class = BeverageCatalogItemSerializer

    @extend_schema(
        tags=["internal"],
        responses={
            200: inline_serializer(
                name="InternalBeveragesResponse",
                fields={
                    "beverages": BeverageCatalogItemSerializer(many=True),
                },
            ),
        },
    )
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

    @extend_schema(
        tags=["internal"],
        responses={
            200: inline_serializer(
                name="InternalCrossDomainResponse",
                fields={
                    "has_insight": drf_serializers.BooleanField(),
                    "insight": inline_serializer(
                        name="InternalCrossDomainInsight",
                        fields={
                            "shown_id": drf_serializers.CharField(),
                            "rule_slug": drf_serializers.CharField(),
                            "nutrition_trigger": drf_serializers.CharField(),
                            "service_category_slug": drf_serializers.CharField(),
                            "insight_text": drf_serializers.CharField(),
                            "rationale_text": drf_serializers.CharField(),
                            "disclaimer_text": drf_serializers.CharField(),
                            "data_points": drf_serializers.IntegerField(),
                            "severity": drf_serializers.CharField(),
                        },
                        required=False,
                    ),
                },
            ),
        },
    )
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

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: inline_serializer(
                name="InternalCrossDomainSeenResponse",
                fields={
                    "shown_id": drf_serializers.CharField(),
                    "seen_at": drf_serializers.DateTimeField(),
                },
            ),
            404: OpenApiResponse(description="Cross-domain row not found"),
        },
    )
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
    serializer_class = CrossDomainDismissRequestSerializer

    @extend_schema(
        tags=["internal"],
        request=CrossDomainDismissRequestSerializer,
        responses={
            200: inline_serializer(
                name="InternalCrossDomainDismissResponse",
                fields={
                    "shown_id": drf_serializers.CharField(),
                    "user_action": drf_serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description="Invalid action"),
            404: OpenApiResponse(description="Cross-domain row not found"),
        },
    )
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
    serializer_class = CrossDomainConvertRequestSerializer

    @extend_schema(
        tags=["internal"],
        request=CrossDomainConvertRequestSerializer,
        responses={
            200: inline_serializer(
                name="InternalCrossDomainConvertResponse",
                fields={
                    "shown_id": drf_serializers.CharField(),
                    "appointment_id": drf_serializers.CharField(),
                    "user_action": drf_serializers.CharField(),
                },
            ),
            400: OpenApiResponse(description="appointment_id required"),
            404: OpenApiResponse(description="Cross-domain row or appointment not found"),
        },
    )
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
    serializer_class = CrossDomainHistoryResponseSerializer

    @extend_schema(
        tags=["internal"],
        responses={200: CrossDomainHistoryResponseSerializer},
    )
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


class InternalBodyParametersEraseView(InternalProfileView):
    """DELETE /api/v1/nutrition/internal/profile/body-parameters/ (DRF-1698).

    Отзыв согласия на персональный расчёт (пакет владельца 12.09 §2):
    шесть параметров стираются, ориентиры инвалидируются, история дневника
    остаётся — см. ``personal_calculation_withdrawal``. Идемпотентно:
    повтор и «профиля не было» — тот же 200, в теле сказано, что стёрто.

    Наследует резолв субъекта и сторож ``IsServiceAccount`` у профиля:
    это та же поверхность, тот же актор (бот от имени проверенного
    клиента), только глагол обратный.
    """

    http_method_names = ["delete"]

    @extend_schema(
        operation_id="internal_nutrition_body_parameters_erase",
        tags=["internal"],
        request=None,
        responses={
            200: OpenApiResponse(description="Erased (idempotent; also when no profile)"),
            400: OpenApiResponse(description="X-External-User-ID invalid"),
        },
    )
    def delete(self, request: Request) -> Response:
        from nutrition.services.personal_calculation_withdrawal import (
            IncompleteErasure,
            erase_personal_calculation_inputs,
        )

        user, err, external_user_id = self._resolve(request)
        if err is not None:
            return err
        try:
            outcome = erase_personal_calculation_inputs(user)
        except IncompleteErasure as exc:
            # Откачено целиком; 500, а не 200 — «удалено» сказать нельзя.
            logger.error(
                "nutrition.body_parameters.erase_incomplete user=%s detail=%s",
                user.pk, exc,
            )
            return error_response(
                "INTERNAL_ERROR", "Erasure incomplete; rolled back.", status_code=500,
            )
        logger.info(
            "nutrition.body_parameters.erased user=%s existed=%s targets_cleared=%s request_id=%s",
            user.pk, outcome.profile_existed, outcome.targets_cleared,
            getattr(request, "request_id", "-"),
        )
        return success_response({
            "erased": list(outcome.erased),
            "targets_cleared": outcome.targets_cleared,
            "profile_existed": outcome.profile_existed,
        })


# ---------------------------------------------------------------------------
# Избранные блюда — DRF-2092 (дневник F12)
# ---------------------------------------------------------------------------


class InternalSavedMealsView(APIView):
    """GET/POST /api/v1/nutrition/internal/saved-meals/ — избранное под субъектом.

    Серверный источник: тот же ``X-External-User-ID`` из новой сессии видит
    тот же список — избранное переживает переустановку. Чужого здесь не
    видно по построению: каждый запрос отфильтрован по субъекту.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"
    serializer_class = SavedMealCreateSerializer

    @extend_schema(
        tags=["internal"],
        responses={
            200: inline_serializer(
                name="InternalSavedMealsListResponse",
                fields={"items": SavedMealSerializer(many=True)},
            ),
        },
    )
    def get(self, request: Request) -> Response:
        from nutrition.services.saved_meal_service import SavedMealService

        user, refusal = _food_log_actor(request)
        if refusal is not None:
            return refusal
        items = SavedMealService().list_for(user)
        return success_response({"items": SavedMealSerializer(items, many=True).data})

    @extend_schema(
        tags=["internal"],
        request=SavedMealCreateSerializer,
        responses={
            201: SavedMealSerializer,
            200: OpenApiResponse(description="То же блюдо с той же порцией уже сохранено"),
            400: OpenApiResponse(description="Validation"),
            404: OpenApiResponse(description="food_log_id не принадлежит субъекту"),
        },
    )
    def post(self, request: Request) -> Response:
        from nutrition.services.saved_meal_service import (
            SavedMealService,
            SourceFoodLogNotFoundError,
        )

        user, refusal = _food_log_actor(request)
        if refusal is not None:
            return refusal
        serializer = SavedMealCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(
                "VALIDATION_ERROR", "Невалидные данные", details=serializer.errors,
            )
        data = serializer.validated_data
        service = SavedMealService()
        try:
            if data.get("food_log_id") is not None:
                outcome = service.save_from_food_log(user, data["food_log_id"])
            else:
                outcome = service.save(
                    user,
                    dish_name=data["dish_name"],
                    portion_g=data["portion_g"],
                    calories=data.get("calories", 0.0),
                    protein_g=data.get("protein_g"),
                    fat_g=data.get("fat_g"),
                    carbs_g=data.get("carbs_g"),
                )
        except SourceFoodLogNotFoundError:
            # «Не найдено», а не «чужое»: по коду нельзя перебирать чужие id.
            return error_response(
                "NOT_FOUND", "Запись не найдена", status_code=status.HTTP_404_NOT_FOUND,
            )
        return success_response(
            SavedMealSerializer(outcome.meal).data,
            status_code=status.HTTP_201_CREATED if outcome.created else status.HTTP_200_OK,
        )


class InternalSavedMealDetailView(APIView):
    """DELETE /api/v1/nutrition/internal/saved-meals/{id}/ — скрыть из списка."""

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    @extend_schema(
        tags=["internal"],
        request=None,
        responses={
            200: inline_serializer(
                name="InternalSavedMealDeleteResponse",
                fields={
                    "id": drf_serializers.UUIDField(),
                    "deleted": drf_serializers.BooleanField(),
                },
            ),
            404: OpenApiResponse(description="Нет такой живой строки у субъекта"),
        },
    )
    def delete(self, request: Request, pk: UUID) -> Response:
        from nutrition.services.saved_meal_service import (
            SavedMealNotFoundError,
            SavedMealService,
        )

        user, refusal = _food_log_actor(request)
        if refusal is not None:
            return refusal
        try:
            meal = SavedMealService().soft_delete(user, pk)
        except SavedMealNotFoundError:
            return error_response(
                "NOT_FOUND", "Запись не найдена", status_code=status.HTTP_404_NOT_FOUND,
            )
        return success_response({"id": str(meal.id), "deleted": True})


# ---------------------------------------------------------------------------
# Дневник по дням за период — DRF-2099 (F10, §48 п.7)
# ---------------------------------------------------------------------------


class InternalDiaryDaysView(APIView):
    """GET /api/v1/nutrition/internal/diary/days/?from=&to= — «N из 7 дней».

    Одна строка на каждый день периода, пустые — явно; ≤ 28 дней; без
    параметров — семь дней до сегодня по поясу человека. Пояс называется в
    ответе, чтобы бот и экран не гадали, чьи это сутки. Ретеншн-механики
    (напоминания, стрики, «пропущено») здесь нет по решению владельца.
    """

    permission_classes = [IsServiceAccount]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "food_scan_internal"

    @extend_schema(
        tags=["internal"],
        responses={
            200: inline_serializer(
                name="InternalDiaryDaysResponse",
                fields={
                    "timezone": drf_serializers.CharField(),
                    "from": drf_serializers.DateField(),
                    "to": drf_serializers.DateField(),
                    "days": inline_serializer(
                        name="InternalDiaryDayRow",
                        fields={
                            "date": drf_serializers.DateField(),
                            "meals_count": drf_serializers.IntegerField(),
                            "kcal": drf_serializers.FloatField(allow_null=True),
                            "has_entries": drf_serializers.BooleanField(),
                        },
                        many=True,
                    ),
                },
            ),
            400: OpenApiResponse(description="Bad from/to or span over 28 days"),
        },
    )
    def get(self, request: Request) -> Response:
        from nutrition.services.diary_days_service import (
            BadPeriod,
            SpanTooLong,
            default_period,
            diary_days,
            person_timezone,
            timezone_name,
        )

        user, refusal = _food_log_actor(request)
        if refusal is not None:
            return refusal
        tz = person_timezone(user.pk)
        raw_from = request.query_params.get("from")
        raw_to = request.query_params.get("to")
        try:
            if raw_from is None and raw_to is None:
                date_from, date_to = default_period(tz)
            else:
                if raw_from is None or raw_to is None:
                    raise BadPeriod("from and to go together")
                date_from = datetime.strptime(raw_from, "%Y-%m-%d").date()
                date_to = datetime.strptime(raw_to, "%Y-%m-%d").date()
            rows = diary_days(user, date_from=date_from, date_to=date_to, tz=tz)
        except (ValueError, BadPeriod, SpanTooLong) as exc:
            return error_response("VALIDATION_ERROR", f"Невалидный период: {exc}")
        return success_response({
            "timezone": timezone_name(tz),
            "from": date_from.isoformat(),
            "to": date_to.isoformat(),
            "days": [row.as_dict() for row in rows],
        })
