"""DRF-2214 PR-2 — выгрузка по ст. 14 несёт то, что каталог запомнил о человеке.

«Забудь всё» стирает цели, план, профиль питания, дневник с фото и историю
подсказок (:data:`users.forget_all_catalog.REMEMBERED_SCOPE`). Выгрузка C5.1 —
то, по чему человек узнаёт, ЧТО о нём хранится, — несла только профиль,
личный профиль предпочтений и профиль мастера. Стереть можно было то, чего
человек в своей выгрузке не видел.

Правило: **что стирается — то выгружается.** Раздел на каждое слово словаря
стирания, с тем же именем (сторож — ``users/tests/test_personal_data_export_remembered_2214.py``).

# Поля — закрытым списком на модель (прецедент DRF-1918)

Каждое конкретное поле каждой модели либо выгружается (``EXPORTED``: поле →
ключ ответа), либо исключено с причиной (``EXCLUDED``). Новое поле модели без
решения краснит сторож: новые данные о здоровье не уходят молча мимо
выгрузки, служебное не уходит молча в неё. Спорное решено в пользу полноты
(слово главного окна по DRF-2214): сырой ответ модели распознавания о фото
выгружается — с пометкой, что это сырой ответ модели.

Фото — ссылкой (``image_url``, как ``avatar_url`` мастера), не байтами:
архивы вне пилотного контракта C5.1.

Выгрузка ничего не создаёт: нет строк — пустые списки и ``null``.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

_KEY = "служебный ключ строки"
_OWNER = "субъект выгрузки — его user_id уже в ответе"
_NEST = "вложенность: строка выгружена внутри родителя"
_TENANT = "ссылка на организацию, не данные о человеке"
_REPEAT = "служебный ключ повтора запроса, не данные о человеке"
_UPDATED = "служебная отметка изменения строки"
_TELEMETRY = "техническая телеметрия распознавания (провайдер, время, стоимость, ошибка), не данные о человеке"
_MODEL_TELEMETRY = "техническая телеметрия модели (токены, задержка), не данные о человеке"

#: Пометка к сырому ответу модели — в самом ответе, рядом с разделом.
RAW_MODEL_RESPONSE_NOTE = (
    "raw_model_response — сырой ответ модели распознавания о фото, как его вернул "
    "провайдер; разобранное из него — в полях dish_name, confidence, portion_g, "
    "ingredients, nutrition"
)


def _same(*names: str) -> dict[str, str]:
    return {name: name for name in names}


_NUTRIENTS = (
    "vitamin_d_iu", "vitamin_b12_mcg", "vitamin_c_mg", "iron_mg",
    "calcium_mg", "magnesium_mg", "omega3_g", "fiber_g",
)

#: Модель → (выгружаемые поля: поле → ключ, исключённые: поле → причина).
FIELDS: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "goals.ClientGoal": (
        _same("goal_key", "goal_text", "selected_at", "source_channel", "state",
              "state_changed_at", "target_date", "created_at"),
        {"id": _KEY, "client": _OWNER, "updated_at": _UPDATED},
    ),
    "goals.GoalAnketaRun": (
        {**_same("started_at", "completed_at"), "goal": "goal_key"},
        {"id": _KEY, "client": _OWNER},
    ),
    "goals.GoalAnketaAnswer": (
        {**_same("step_key", "option_key", "option_keys", "answer_text"), "created_at": "answered_at"},
        {"id": _KEY, "run": _NEST},
    ),
    "wellness.DesiredOutcome": (
        _same("target", "statement_text", "direction", "desired_state_numeric", "status",
              "created_at", "closed_at"),
        {"id": _KEY, "user": _OWNER},
    ),
    "wellness.PersonalPlan": (
        _same("status", "goal_key", "source", "created_at", "closed_at"),
        {"id": _KEY, "user": _OWNER,
         "goal": "ссылка на цель — её goal_key выгружен здесь и в разделе goals"},
    ),
    "wellness.PlanAction": (
        _same("action_type", "cadence", "target_count", "created_at"),
        {"id": _KEY, "plan": _NEST},
    ),
    "wellness.PlanOutcomeLink": (
        {**_same("target_date", "status", "created_at", "closed_at"), "outcome": "outcome_target"},
        {"id": _KEY, "plan": _NEST},
    ),
    "wellness.ProgressObservation": (
        {**_same("observation_type", "origin", "instrument", "value_numeric", "value_ordinal",
                 "observed_at", "created_at"),
         "superseded_by": "is_superseded"},
        {"id": _KEY, "user": _OWNER},
    ),
    "nutrition.NutritionProfile": (
        _same(
            "gender", "age", "height_cm", "weight_kg", "weight_range", "timezone",
            "activity_coefficient", "goal", "pace", "diet_preference", "health_flags",
            "bmr", "daily_kcal", "daily_protein_g", "daily_fat_g", "daily_carbs_g",
            "daily_water_ml", "daily_vitamin_d_iu", "daily_vitamin_b12_mcg", "daily_vitamin_c_mg",
            "daily_iron_mg", "daily_calcium_mg", "daily_magnesium_mg", "daily_omega3_g",
            "daily_fiber_g", "targets_source", "targets_input_snapshot", "targets_computed_at",
            "targets_confirmed_at", "calories_source", "fluids_source", "calories_confirmed_at",
            "fluids_confirmed_at", "pending_proposal", "goal_overridden_by",
            "bmi_warning_overridden_at", "last_overrides_applied", "disclaimer_acked",
            "onboarded_at", "first_food_logged_at", "weekly_summary_unlocked_at", "created_at",
        ),
        {"user": _OWNER, "tenant": _TENANT, "updated_at": _UPDATED,
         "targets_method_versions": "версии методик расчёта ориентиров, не данные о человеке"},
    ),
    "nutrition.FoodLog": (
        {**_same("dish_name", "portion_multiplier", "calories", "protein_g", "fat_g", "carbs_g",
                 *_NUTRIENTS, "micronutrients_source", "meal_type", "entry_origin", "logged_at",
                 "created_at"),
         "scan": "from_scan"},
        {"id": _KEY, "user": _OWNER, "idempotency_key": _REPEAT},
    ),
    "nutrition.FoodScan": (
        {**_same("dish_name", "confidence", "portion_g", "ingredients", "nutrition", "created_at"),
         "image": "image_url", "raw_response": "raw_model_response"},
        {"id": _KEY, "user": _OWNER, "tenant": _TENANT,
         "provider_used": _TELEMETRY, "provider_fallback_from": _TELEMETRY,
         "latency_ms": _TELEMETRY, "provider_usage": _TELEMETRY,
         "provider_cost_usd": _TELEMETRY, "error_code": _TELEMETRY, "error_message": _TELEMETRY},
    ),
    "nutrition.WaterEntry": (
        {**_same("ts", "ml", "water_ml", "kcal", "protein_g", "fat_g", "carbs_g", "sugar_g",
                 "caffeine_mg", *_NUTRIENTS, "milestone_threshold", "deleted_at",
                 "deleted_reason", "created_at"),
         "beverage": "beverage", "food_log": "from_food_log"},
        {"id": _KEY, "user": _OWNER, "tenant": _TENANT, "idempotency_key": _REPEAT},
    ),
    "nutrition.WaterLog": (
        _same("amount_ml", "logged_at", "created_at"),
        {"id": _KEY, "user": _OWNER},
    ),
    "nutrition.SavedMeal": (
        {**_same("dish_name", "portion_g", "calories", "protein_g", "fat_g", "carbs_g",
                 "created_at", "deleted_at"),
         "source_food_log": "from_food_log"},
        {"id": _KEY, "user": _OWNER},
    ),
    "nutrition.DeletedFoodLog": (
        _same("snapshot", "deleted_at"),
        {"id": "тот же ключ, что был у удалённой записи дневника; служебный", "user": _OWNER},
    ),
    "nutrition.CrossDomainShownRule": (
        {**_same("nutrition_trigger", "service_category_slug", "shown_at", "seen_at",
                 "surface", "user_action"),
         "rule": "rule_id", "appointment": "appointment_id"},
        {"id": _KEY, "user": _OWNER},
    ),
    # DRF-2277 — история уведомлений и ИИ-чат приложения (решение владельца §72 п.3).
    "notifications.Notification": (
        _same("template_id", "channel", "title", "body", "data", "deep_link", "status",
              "is_read", "created_at", "sent_at"),
        {"id": _KEY, "user": _OWNER,
         "error": "техническая ошибка доставки (транспорт пуша/SMS), не данные о человеке"},
    ),
    "ai.Conversation": (
        _same("is_active", "deleted_at", "last_message_at", "created_at"),
        {"id": _KEY, "user": _OWNER, "tenant": _TENANT},
    ),
    "ai.Message": (
        # Сырой вызов инструмента модели — как сырой ответ распознавания (DRF-2214):
        # спорное решено в пользу полноты, с пометкой.
        {**_same("role", "content", "action_type", "action_data", "created_at"),
         "tool_call": "raw_tool_call"},
        {"id": _KEY, "conversation": _NEST,
         "tool_call_id": "служебная связь вызова инструмента с его результатом",
         "tokens_in": _MODEL_TELEMETRY, "tokens_out": _MODEL_TELEMETRY,
         "latency_ms": _MODEL_TELEMETRY},
    ),
}


#: Стирается «забудь всё», но в выгрузку не идёт — с причиной. Только служебные
#: копии того, что уже выгружено своим разделом (решение главного окна по
#: DRF-2214 для той же формы — ``NutritionOutboxEvent``).
DECLARED_NOT_EXPORTED: dict[str, str] = {
    "nutrition.ProfileIdempotencyKey": (
        "суточный служебный кэш ответа на запись профиля питания — копия раздела "
        "nutrition_profile, выгруженного целиком"
    ),
    # DRF-2277 — решение главного окна (#545).
    "nutrition.NutritionOutboxEvent": (
        "очередь вебхуков в бот — служебная копия профиля питания и дневника "
        "(вода, рубежи, паттерны, распознавание), выгруженных своими разделами"
    ),
}

#: Пометка к сырому вызову инструмента модели в ИИ-чате — рядом с разделом.
RAW_TOOL_CALL_NOTE = (
    "raw_tool_call — сырой вызов инструмента моделью ИИ-чата, как его вернул "
    "провайдер; разобранное из него — в полях action_type и action_data"
)


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


#: Поле-связь → как её выгрузить (без второго запроса там, где хватает id).
_CONVERT = {
    ("goals.GoalAnketaRun", "goal"): lambda o: o.goal.goal_key if o.goal_id else None,
    ("wellness.PlanOutcomeLink", "outcome"): lambda o: o.outcome.target,
    ("wellness.ProgressObservation", "superseded_by"): lambda o: o.superseded_by_id is not None,
    ("nutrition.FoodLog", "scan"): lambda o: o.scan_id is not None,
    ("nutrition.FoodScan", "image"): lambda o: o.image.url if o.image else None,
    ("nutrition.WaterEntry", "beverage"): lambda o: o.beverage.name_ru if o.beverage_id else None,
    ("nutrition.WaterEntry", "food_log"): lambda o: o.food_log_id is not None,
    ("nutrition.SavedMeal", "source_food_log"): lambda o: o.source_food_log_id is not None,
    ("nutrition.CrossDomainShownRule", "rule"): lambda o: o.rule.rule_id,
    ("nutrition.CrossDomainShownRule", "appointment"): lambda o: (
        str(o.appointment_id) if o.appointment_id else None
    ),
}


def _row(obj) -> dict:
    label = obj._meta.label
    exported, _ = FIELDS[label]
    out = {}
    for field, key in exported.items():
        convert = _CONVERT.get((label, field))
        out[key] = convert(obj) if convert else _plain(getattr(obj, field))
    return out


def export_goals(user) -> dict:
    from goals.models import ClientGoal, GoalAnketaRun

    goals = [_row(g) for g in ClientGoal.objects.filter(client=user).order_by("created_at", "id")]
    runs = []
    for run in (
        GoalAnketaRun.objects.filter(client=user)
        .select_related("goal")
        .prefetch_related("answers")
        .order_by("started_at", "id")
    ):
        item = _row(run)
        item["answers"] = [_row(a) for a in sorted(run.answers.all(), key=lambda a: (a.created_at, a.pk))]
        runs.append(item)
    return {"goals": goals, "anketa_runs": runs}


def export_wellness_plan(user) -> dict:
    from wellness.models import DesiredOutcome, PersonalPlan, ProgressObservation

    plans = []
    for plan in PersonalPlan.objects.filter(user=user).order_by("created_at", "id"):
        item = _row(plan)
        item["actions"] = [_row(a) for a in plan.actions.order_by("created_at", "id")]
        item["outcome_links"] = [
            _row(link) for link in plan.outcome_links.select_related("outcome").order_by("created_at", "id")
        ]
        plans.append(item)
    return {
        "desired_outcomes": [
            _row(o) for o in DesiredOutcome.objects.filter(user=user).order_by("created_at", "id")
        ],
        "plans": plans,
        "progress_observations": [
            _row(o) for o in ProgressObservation.objects.filter(user=user).order_by("observed_at", "id")
        ],
    }


def export_nutrition_profile(user) -> dict | None:
    from nutrition.models import NutritionProfile

    profile = NutritionProfile.objects.filter(user=user).first()
    return _row(profile) if profile is not None else None


def export_food_diary(user) -> dict:
    from nutrition.models import DeletedFoodLog, FoodLog, FoodScan, SavedMeal, WaterEntry, WaterLog

    return {
        "food_logs": [_row(x) for x in FoodLog.objects.filter(user=user).order_by("logged_at", "id")],
        "food_scans": [_row(x) for x in FoodScan.objects.filter(user=user).order_by("created_at", "id")],
        "water_entries": [
            _row(x) for x in WaterEntry.objects.filter(user=user).select_related("beverage").order_by("ts", "id")
        ],
        "water_logs": [_row(x) for x in WaterLog.objects.filter(user=user).order_by("logged_at", "id")],
        "saved_meals": [_row(x) for x in SavedMeal.objects.filter(user=user).order_by("created_at", "id")],
        "deleted_food_logs": [
            _row(x) for x in DeletedFoodLog.objects.filter(user=user).order_by("deleted_at", "id")
        ],
        "notes": {"raw_model_response": RAW_MODEL_RESPONSE_NOTE},
    }


def export_shown_hints(user) -> list[dict]:
    from nutrition.models import CrossDomainShownRule

    return [
        _row(x)
        for x in CrossDomainShownRule.objects.filter(user=user).select_related("rule").order_by("shown_at", "id")
    ]


def export_notification_history(user) -> list[dict]:
    """Все строки уведомлений человека — и обезличенные маркеры битов (DRF-2277):
    выгрузка называет всё, что хранится."""
    from notifications.models import Notification

    return [_row(x) for x in Notification.objects.filter(user=user).order_by("created_at", "id")]


def export_app_ai_chat(user) -> dict:
    """ИИ-чат приложения — беседы с сообщениями, включая мягко удалённые (DRF-2277)."""
    from ai.models import Conversation

    conversations = []
    for conv in Conversation.all_objects.filter(user=user).order_by("created_at", "id"):
        item = _row(conv)
        item["messages"] = [_row(m) for m in conv.messages.order_by("created_at", "id")]
        conversations.append(item)
    return {"conversations": conversations, "notes": {"raw_tool_call": RAW_TOOL_CALL_NOTE}}


def export_remembered(user) -> dict:
    """Все разделы запомненного — по слову словаря стирания на раздел."""
    return {
        "goals": export_goals(user),
        "wellness_plan": export_wellness_plan(user),
        "nutrition_profile": export_nutrition_profile(user),
        "food_diary": export_food_diary(user),
        "shown_hints": export_shown_hints(user),
        "notification_history": export_notification_history(user),
        "app_ai_chat": export_app_ai_chat(user),
    }
