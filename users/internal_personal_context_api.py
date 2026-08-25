"""Internal Bearer API — personal-context read/write for the bot (A1a).

Контракт: ai-bot-platform/docs/plans/2026-07-03-PERSONAL_CONTEXT_INTERNAL_API_SPEC.md

Ayla — единственный владелец памяти. Бот ходит сюда по служебному Bearer-токену
(``IsInternalBearer``, как #1016 catalog), резолвит человека по ``ayla_user_id``:

    GET    /api/v1/internal/users/{ayla_user_id}/personal-context/
    PATCH  /api/v1/internal/users/{ayla_user_id}/personal-context/
    DELETE /api/v1/internal/users/{ayla_user_id}/personal-context/
    GET    /api/v1/internal/users/{ayla_user_id}/personal-context/ask-eligibility/
    POST   /api/v1/internal/users/{ayla_user_id}/personal-context/mark-asked/
    POST   /api/v1/internal/users/{ayla_user_id}/personal-context/skip/

Пилот = зелёная зона. 8 anti-spam правил остаются в ``personalization_engine``
(бот их не дублирует, спрашивает ask-eligibility). Consent-гейт (memory_green)
enforce'ится на боте ДО вызова (см. MEMORY_CONSENT_SPEC); Ayla-side backstop —
follow-up (A1b), сейчас доверяем internal-Bearer вызову.

Scope A1a: green-зона, без шифрования/zone-поля (A1b) и без per-field confidence
persist (принимаем в PATCH, source пишем в data_sources; confidence — с A1b/MemoryFact).

DRF-1367 — DELETE это глагол стирания «забудь всё». До него у контракта не
было ни одного способа сказать «сотри профиль»: только PATCH по именам
полей, и цену через него нельзя очистить в принципе (``null`` → 400,
пустая строка падает на Decimal-колонке). Мост называл три поля из
двенадцати, и список расходился с моделью при каждом новом поле. Глагол
не перечисляет полей — он спрашивает модель (см. ``personal_context_erasure``).

Владелец, ``Ayla/docs/OD_MEMORY.md`` §1: источник истины — бэкенд. Значит
операция «стереть то, чем владеешь» обязана быть здесь, а не на мосте.

Происхождение (P0-3): GET и PATCH отдают `data_sources` рядом с `context`.
Бот рендерит эти значения прямо в промпт LLM, и без происхождения выведенный
`busy_days` приходил модели неотличимым от того, что человек ввёл сам. Поле
хранилось в строке с самого начала — терялось ровно здесь, на границе API.
"""
from __future__ import annotations

import uuid as uuid_mod

from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users import personalization_engine as engine
from users.models import User, UserPersonalContext
from users.permissions import IsInternalBearer
from users.personal_context_erasure import erase_personal_context
from users.personal_context_views import _GREEN_ZONE_FIELDS
from users.response import error_response, success_response

# Поля, о которых concierge может задать вопрос (subset green), + prompt-hint.
# Порядок = приоритет предложения следующего вопроса.
_ASK_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("preferred_time_slots", "Тебе удобнее записываться утром, днём или вечером?"),
    ("price_range_max", "На какой бюджет ориентироваться?"),
    ("favorite_masters", "Есть любимый мастер, к кому вернуться?"),
    ("workplace_district", "Искать рядом с работой — в каком районе работаешь?"),
    ("home_district", "Или удобнее рядом с домом — какой район?"),
    ("busy_days", "Есть дни, которые лучше избегать?"),
    ("min_rating_preference", "Важен минимальный рейтинг мастера?"),
    ("diet_type", "Придерживаешься какой-то диеты?"),
)

_SOURCE_CHOICES = ("explicit", "behavioral", "conversational", "transactional")
_MAX_BATCH = 10


def _resolve_user(ayla_user_id: str) -> User | None:
    """Resolve the subject. A soft-deleted account counts as gone.

    DRF-1368 — this resolver had no ``deleted_at`` filter while its
    neighbour ``personal_data_api._get_live_user`` did, so the internal GET
    the prompt block is built from answered 200 for a deleted person. Two
    adjacent resolvers, two different answers to «does this human exist».
    """
    try:
        uuid_mod.UUID(str(ayla_user_id))
    except (ValueError, TypeError):
        return None
    return User.objects.filter(id=ayla_user_id, deleted_at__isnull=True).first()


def _green_context(ctx: UserPersonalContext) -> dict:
    return {f: getattr(ctx, f) for f in _GREEN_ZONE_FIELDS}


def _green_data_sources(ctx: UserPersonalContext) -> dict:
    """Per-field origin for every green field — «кто это сказал».

    P0-3 (`REPORT_AYLA_MEMORY_VS_KB.md` §24): the bot renders these values
    straight into an LLM prompt, and until now it received values WITHOUT
    origin. So `busy_days` — computed nightly from booking history by
    `personal_context_inference` and stamped `data_sources["busy_days"] =
    "inferred"` — arrived indistinguishable from a preference the person
    typed, and the model could quote a guess back as a confirmed fact.
    The provenance existed in the row all along; this endpoint dropped it.

    Every green field gets an entry, `explicit` by default: an unstamped
    field is a legacy mobile-app write, i.e. something the person typed.
    Emitting the complete map (rather than only the stamped subset) means
    a future inference pass that forgets to stamp cannot silently arrive
    looking user-stated — the consumer's rule is simply «not `explicit`
    ⇒ derived».
    """
    stamped = ctx.data_sources or {}
    return {f: stamped.get(f) or "explicit" for f in _GREEN_ZONE_FIELDS}


class _UpdateItemSerializer(serializers.Serializer):
    field = serializers.ChoiceField(choices=_GREEN_ZONE_FIELDS)
    value = serializers.JSONField()
    source = serializers.ChoiceField(choices=_SOURCE_CHOICES, default="explicit")
    confidence = serializers.FloatField(
        min_value=0.0, max_value=1.0, default=1.0, required=False
    )


class InternalPersonalContextView(APIView):
    """GET / PATCH персонального контекста по ``ayla_user_id`` (Bearer)."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    def get(self, request: Request, ayla_user_id: str) -> Response:
        user = _resolve_user(ayla_user_id)
        if user is None:
            return error_response("USER_NOT_FOUND", "User not found.", status_code=404)
        # Пустой контекст → 200 + пустой объект (lazy-create). Бот не обрабатывает
        # 404 как особый кейс — новый пользователь = нормальное пустое состояние.
        ctx, _ = UserPersonalContext.objects.get_or_create(user=user)
        # Truthy = осмысленно заполнено (False/0/[]/"" = дефолт, не «заполнено»).
        filled = sum(1 for f in _GREEN_ZONE_FIELDS if getattr(ctx, f))
        return success_response({
            "ayla_user_id": str(user.id),
            "context": _green_context(ctx),
            # Additive sibling of `context`, same keys (contract v1.0 §forward-compat:
            # unknown keys ride along untouched, so old bots ignore it).
            "data_sources": _green_data_sources(ctx),
            "meta": {"filled_fields": filled, "updated_at": ctx.updated_at.isoformat()},
        })

    def patch(self, request: Request, ayla_user_id: str) -> Response:
        user = _resolve_user(ayla_user_id)
        if user is None:
            return error_response("USER_NOT_FOUND", "User not found.", status_code=404)

        updates = request.data.get("updates")
        if not isinstance(updates, list) or not updates:
            return error_response(
                "VALIDATION_ERROR", "Body must contain a non-empty 'updates' list.",
                status_code=400,
            )
        if len(updates) > _MAX_BATCH:
            return error_response(
                "TOO_MANY_FIELDS", f"At most {_MAX_BATCH} fields per request.",
                status_code=400,
            )
        ser = _UpdateItemSerializer(data=updates, many=True)
        ser.is_valid(raise_exception=True)

        ctx, _ = UserPersonalContext.objects.get_or_create(user=user)
        data_sources = dict(ctx.data_sources or {})
        for item in ser.validated_data:
            setattr(ctx, item["field"], item["value"])
            # A1a: пишем provenance (source). Per-field confidence — с A1b (MemoryFact).
            data_sources[item["field"]] = item["source"]
        ctx.data_sources = data_sources
        ctx.save()
        return success_response({
            "ayla_user_id": str(user.id),
            "context": _green_context(ctx),
            "data_sources": _green_data_sources(ctx),
        })

    def delete(self, request: Request, ayla_user_id: str) -> Response:
        """DRF-1367 — «забудь всё». Одна операция вместо перечисления полей.

        Идемпотентна: повтор возвращает 200 с пустым ``erased``. Ответ
        отдаёт контекст после стирания, чтобы бот не ходил вторым запросом
        проверять, что получилось, — все двенадцать полей в дефолте.
        """
        user = _resolve_user(ayla_user_id)
        if user is None:
            return error_response("USER_NOT_FOUND", "User not found.", status_code=404)

        scope = erase_personal_context(user, initiator="bot_forget_all")
        ctx, _ = UserPersonalContext.objects.get_or_create(user=user)
        return success_response({
            "ayla_user_id": str(user.id),
            "erased": scope,
            "context": _green_context(ctx),
        })


class InternalAskEligibilityView(APIView):
    """Обёртка над 8 правилами: какое ОДНО поле спросить (или нельзя)."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    def get(self, request: Request, ayla_user_id: str) -> Response:
        user = _resolve_user(ayla_user_id)
        if user is None:
            return error_response("USER_NOT_FOUND", "User not found.", status_code=404)

        blocked_reason = "no_candidate"
        for field, hint in _ASK_CANDIDATES:
            verdict = engine.should_ask_question(user, field)
            if verdict.allowed:
                return success_response({
                    "should_ask": True,
                    "field": field,
                    "reason_code": None,
                    "explain": verdict.explanation(),
                    "prompt_hint": hint,
                })
            # first_interaction — глобальный блок, нет смысла перебирать дальше.
            if verdict.reason == "first_interaction":
                blocked_reason = verdict.reason
                break
            if blocked_reason == "no_candidate":
                blocked_reason = verdict.reason
        return success_response({"should_ask": False, "blocked_by": blocked_reason})


class _FieldBodySerializer(serializers.Serializer):
    field = serializers.ChoiceField(choices=[f for f, _ in _ASK_CANDIDATES])


class InternalMarkAskedView(APIView):
    """POST — отметить, что вопрос по полю ЗАДАН (24ч cooldown)."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    def post(self, request: Request, ayla_user_id: str) -> Response:
        user = _resolve_user(ayla_user_id)
        if user is None:
            return error_response("USER_NOT_FOUND", "User not found.", status_code=404)
        body = _FieldBodySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        engine.mark_asked(user, body.validated_data["field"])
        return success_response({"ok": True})


class InternalSkipView(APIView):
    """POST — пользователь пропустил вопрос (anti-spam, skip x2 → пауза)."""

    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    def post(self, request: Request, ayla_user_id: str) -> Response:
        user = _resolve_user(ayla_user_id)
        if user is None:
            return error_response("USER_NOT_FOUND", "User not found.", status_code=404)
        body = _FieldBodySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        count = engine.mark_skipped(user, body.validated_data["field"])
        return success_response({"ok": True, "skip_count": count})
