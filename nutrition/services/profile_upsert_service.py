"""GET/POST handlers for ``/api/v1/nutrition/internal/profile/`` (DRF-300).

Sits between the views and ``NutritionProfile``/``compute_norms``. Owns:

- PATCH-semantics upsert: only fields present in the request mutate
- ``_skipped_fields``-driven flag flips (``weight_skipped``, etc.)
- Idempotency-Key 24h replay cache via ``ProfileIdempotencyKey``
- Recompute & persist post-override norms — ТОЛЬКО с основанием
  (``targets_recompute_gate.recompute_permitted``, §103 N-b); без него
  отказ пишется в ``last_overrides_applied`` и в лог, ориентиры не
  трогаются
- Lifecycle markers (``onboarded_at`` first-flip on ``complete=true``)
- Wire response builder shared with GET
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone as dt_tz
from typing import Any

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction

from nutrition.models import NutritionProfile, ProfileIdempotencyKey
from nutrition.services.nutrition_profile_service import (
    DEFAULT_ACTIVITY,
    ProfileInputs,
    compute_norms,
)
from nutrition.services.outbox_service import enqueue_profile_updated
from nutrition.services.targets_recompute_gate import (
    RECOMPUTE_REFUSED_NO_CONSENT,
    recompute_permitted,
    refusal_record,
)

logger = logging.getLogger(__name__)

IDEMPOTENCY_TTL_HOURS = 24


# ---------------------------------------------------------------------------
# Idempotency cache
# ---------------------------------------------------------------------------


def _cache_get(key: str, user_id: int) -> dict | None:
    if not key:
        return None
    row = ProfileIdempotencyKey.objects.filter(
        key=key, user_id=user_id, expires_at__gt=datetime.now(dt_tz.utc),
    ).first()
    return row.response if row else None


def _cache_set(key: str, user_id: int, response: dict) -> None:
    if not key:
        return
    # Roundtrip through DjangoJSONEncoder so datetimes become ISO strings
    # — matches what DRF would render and keeps the JSONField column
    # JSON-safe across drivers.
    serializable = json.loads(json.dumps(response, cls=DjangoJSONEncoder))
    ProfileIdempotencyKey.objects.update_or_create(
        key=key,
        defaults={
            "user_id": user_id,
            "response": serializable,
            "expires_at": datetime.now(dt_tz.utc) + timedelta(hours=IDEMPOTENCY_TTL_HOURS),
        },
    )


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def get_profile_response(user, external_user_id: str) -> dict:
    profile = NutritionProfile.objects.filter(user=user).first()
    if profile is None:
        return {"external_user_id": external_user_id, "exists": False}
    return _serialize(profile, external_user_id, exists=True)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def upsert_profile(
    *,
    user,
    external_user_id: str,
    payload: dict[str, Any],
    idempotency_key: str | None,
) -> dict:
    cached = _cache_get(idempotency_key or "", user.id)
    if cached is not None:
        return cached

    with transaction.atomic():
        profile, _ = NutritionProfile.objects.select_for_update().get_or_create(
            user=user,
            defaults={"activity_coefficient": DEFAULT_ACTIVITY},
        )
        _apply_patch(profile, payload)
        # §103 N-b: пересчёт — только с основанием. Довод и три сценария
        # — в докстринге ``targets_recompute_gate``. Здесь важно одно:
        # запрет стоит В КОДЕ, а не в очерёдности запусков команд.
        if recompute_permitted(profile, payload):
            _recompute_and_persist(profile)
        else:
            _refuse_recompute(profile)
        _flip_lifecycle_markers(profile, payload)
        profile.save()

    response = _serialize(profile, external_user_id, exists=True)
    _cache_set(idempotency_key or "", user.id, response)
    enqueue_profile_updated(
        external_user_id=external_user_id,
        profile_payload={
            "goal": response["goal"],
            "pace": response["pace"],
            "norms": response["norms"],
            "goal_overridden_by": response["goal_overridden_by"],
            "onboarded_at": response["onboarded_at"],
        },
    )
    return response


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


_DIRECT_FIELDS = (
    "gender", "age", "height_cm", "weight_kg", "weight_range",
    "activity_coefficient", "goal", "pace", "diet_preference", "timezone",
)


def _apply_patch(profile: NutritionProfile, payload: dict) -> None:
    for f in _DIRECT_FIELDS:
        if f in payload:
            setattr(profile, f, payload[f])

    flags = dict(profile.health_flags or {})
    incoming_flags = payload.get("health_flags")
    if incoming_flags:
        flags.update(incoming_flags)

    skipped = payload.get("_skipped_fields") or []
    for field in skipped:
        flags[f"{field}_skipped"] = True

    profile.health_flags = flags

    if "disclaimer_acked" in payload:
        profile.disclaimer_acked = payload["disclaimer_acked"]


def _recompute_and_persist(profile: NutritionProfile) -> None:
    norms = compute_norms(ProfileInputs(
        gender=profile.gender or "female",
        age=profile.age,
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
        activity_coefficient=profile.activity_coefficient or DEFAULT_ACTIVITY,
        goal=profile.goal or "maintain",
        pace=profile.pace or "moderate",
        health_flags=profile.health_flags or {},
    ))
    profile.bmr = norms.bmr
    profile.daily_kcal = norms.daily_kcal
    profile.daily_protein_g = norms.daily_protein_g
    profile.daily_fat_g = norms.daily_fat_g
    profile.daily_carbs_g = norms.daily_carbs_g
    # ``profile.daily_water_ml`` здесь БОЛЬШЕ НЕ ПИШЕТСЯ: формула
    # 30 мл × вес снята (§82, §85). Столбец nullable (§103, миграция
    # 0018) и у новых строк ``NULL``; выход снятой формулы у старых
    # строк стирает команда ``clear_targets_without_provenance``, а не
    # этот пересчёт: команда печатает значения до записи и запускается
    # тем, кем решено, — пересчёт на POST этого не умеет.
    # DRF-265: micronutrient RDA — recomputed on every upsert.
    profile.daily_vitamin_d_iu = norms.daily_vitamin_d_iu
    profile.daily_vitamin_b12_mcg = norms.daily_vitamin_b12_mcg
    profile.daily_vitamin_c_mg = norms.daily_vitamin_c_mg
    profile.daily_iron_mg = norms.daily_iron_mg
    profile.daily_calcium_mg = norms.daily_calcium_mg
    profile.daily_magnesium_mg = norms.daily_magnesium_mg
    profile.daily_omega3_g = norms.daily_omega3_g
    profile.daily_fiber_g = norms.daily_fiber_g
    profile.goal = norms.goal
    profile.pace = norms.pace
    profile.goal_overridden_by = norms.goal_overridden_by
    # DRF-1339 добавлял сюда ``{"reason": "assumed_input", "field":
    # "weight_kg"}`` — маркер того, что нормы посчитаны от
    # ``DEFAULT_WEIGHT_KG``, а не от названного человеком веса. Маркер
    # снят вместе с подстановкой: он был ПРИЗНАНИЕМ, а не отказом, и
    # число всё равно считалось, уезжало в профиль и показывалось.
    #
    # Теперь недостающий вход отменяет расчёт, и запись об этом делает
    # сам ``compute_norms``: ``{"reason": "insufficient_inputs",
    # "fields": [...]}``. Одно имя вместо двух, и оно означает «расчёта
    # нет», а не «расчёт есть, но входы чужие».
    profile.last_overrides_applied = list(norms.overrides_applied)

    # ── Происхождение (DRF-1623 N-d) ────────────────────────────────────
    #
    # Пишется на КАЖДОМ пересчёте, вместе со значением, а не отдельным
    # вызовом: разъехаться они не должны. Ориентир, у которого значение
    # новое, а происхождение старое, хуже, чем ориентир без происхождения
    # — он выглядит объяснённым.
    #
    # Отказ (нехватка входов) не выдаётся за расчёт: снимок пуст, версий
    # нет, источник — «ориентира нет». Прежнее происхождение при этом
    # СТИРАЕТСЯ намеренно: значения обнулены строкой выше, и оставить
    # рядом с нулями объяснение прошлого расчёта значило бы объяснить
    # число, которого больше нет.
    #
    # Состоявшийся расчёт — ПРЕДЛОЖЕНИЕ, а не действующий ориентир
    # (§5.1, 11.09.2026): ``ayla_proposed`` до тех пор, пока человек не
    # подтвердит его через ``confirm_targets``. Каждый новый расчёт —
    # новое предложение: прежнее подтверждение относилось к прежним
    # числам, и переносить его на новые значило бы подтвердить за
    # человека то, чего он не видел. Поэтому ``targets_confirmed_at``
    # стирается вместе с пересчётом, а не только при отказе.
    if norms.computed:
        profile.targets_source = NutritionProfile.TargetsSource.AYLA_PROPOSED
        profile.targets_method_versions = dict(norms.method_versions)
        profile.targets_input_snapshot = dict(norms.input_snapshot)
        profile.targets_computed_at = datetime.now(dt_tz.utc)
        profile.targets_confirmed_at = None
    else:
        profile.targets_source = NutritionProfile.TargetsSource.NONE
        profile.targets_method_versions = {}
        profile.targets_input_snapshot = {}
        profile.targets_computed_at = None
        profile.targets_confirmed_at = None


def _refuse_recompute(profile: NutritionProfile) -> None:
    """Пересчёта не было — и это ВИДНО, а не подразумевается.

    Ориентиры, происхождение, снимок и версии остаются как лежат:
    ``none`` остаётся ``none`` с ``NULL``, ``unknown_legacy`` остаётся
    ``unknown_legacy`` со своими старыми числами до команды очистки.
    Ни то, ни другое не превращается в ``ayla_calculated`` — это и есть
    третье окно, которое сторож закрывает.

    Запись об отказе ДОБАВЛЯЕТСЯ в ``last_overrides_applied``, а не
    заменяет его: у ``unknown_legacy`` там лежит след прежней лестницы
    переопределений, и это аудит, который команда очистки тоже не
    трогает. Повторный отказ запись не дублирует — один отказ, одна
    строка. Прежняя запись отказа снимается тем же путём при следующем
    состоявшемся пересчёте: ``_recompute_and_persist`` пишет список
    заново.

    Лог — одна строка ``warning``: молчаливый отказ дал бы профиль,
    который выглядит обработанным.
    """
    audit = [
        e for e in (profile.last_overrides_applied or [])
        if not (isinstance(e, dict) and e.get("reason") == RECOMPUTE_REFUSED_NO_CONSENT)
    ]
    audit.append(refusal_record(profile))
    profile.last_overrides_applied = audit
    logger.warning(
        "nutrition.targets.recompute_refused user=%s source=%s",
        profile.user_id,
        profile.targets_source,
    )


def _flip_lifecycle_markers(profile: NutritionProfile, payload: dict) -> None:
    if payload.get("complete") and profile.onboarded_at is None:
        profile.onboarded_at = datetime.now(dt_tz.utc)


def _norms_block(profile: NutritionProfile) -> dict[str, Any]:
    """Посчитанные ориентиры — или ПУСТОЙ словарь, если расчёта не было.

    ### Почему пустой словарь, а не нули

    До этой правки блок уезжал целиком и всегда: ``daily_kcal: 0``,
    ``bmr: 0``. Ноль здесь не «ориентир ноль калорий» — такого не бывает,
    — а «расчёта не было», и эти два утверждения потребитель различить не
    мог. Сводка ту же болезнь уже вылечила: ``calories_goal`` уходит
    ``None`` и выбрасывается :class:`OmitAbsentTargetsMixin`. Профиль
    остался последним местом, где отказ выглядел числом.

    Пустой словарь, а не отсутствующий ключ ``norms``: ``{}`` — законный
    ответ «спросили, ориентиров нет», и он отличается от «блок не
    приехал», как пустой список отличается от отсутствующего. Ключ
    ``norms`` читают потребители, и его исчезновение означало бы для них
    сбой чтения, а сбоя нет.

    ### Признак берётся из происхождения, а не из значений

    Условие — ``targets_source``, а не ``daily_kcal > 0``. Разница видна
    на строке, у которой расчёт отменён, а старое число ещё лежит в
    столбце: по значению она выглядит посчитанной, по происхождению —
    нет. Спрашивать надо у того, кто знает, ЧТО СТОИТ за числом, а не у
    самого числа.

    ``daily_water_ml`` не возвращается ни в одной ветке: формула, которая
    его считала, снята (§82, §85), а в столбце у существующих строк ещё
    лежит старое ``30 × вес`` — отдать его значило бы выдать снятую
    методику за живую.
    """
    if profile.targets_source == NutritionProfile.TargetsSource.NONE:
        return {}
    block: dict[str, Any] = {
        "bmr": profile.bmr,
        "daily_kcal": profile.daily_kcal,
        "daily_protein_g": profile.daily_protein_g,
        "daily_fat_g": profile.daily_fat_g,
        "daily_carbs_g": profile.daily_carbs_g,
        # DRF-265: micronutrient RDA targets.
        "daily_vitamin_d_iu": profile.daily_vitamin_d_iu,
        "daily_vitamin_b12_mcg": profile.daily_vitamin_b12_mcg,
        "daily_vitamin_c_mg": profile.daily_vitamin_c_mg,
        "daily_iron_mg": profile.daily_iron_mg,
        "daily_calcium_mg": profile.daily_calcium_mg,
        "daily_magnesium_mg": profile.daily_magnesium_mg,
        "daily_omega3_g": profile.daily_omega3_g,
        "daily_fiber_g": profile.daily_fiber_g,
    }
    # ``daily_water_ml`` едет ТОЛЬКО при ``user_entered`` (§5.1): это
    # единственный источник, при котором в столбце лежит число человека, а
    # не выход снятой формулы 30 × вес. У остальных источников столбец либо
    # NULL (новые строки, очищенные), либо остаток формулы до команды
    # очистки — его отдать значило бы выдать снятую методику за живую.
    if (
        profile.targets_source == NutritionProfile.TargetsSource.USER_ENTERED
        and profile.daily_water_ml is not None
    ):
        block["daily_water_ml"] = profile.daily_water_ml
    # Ручная норма несёт только то, что человек назвал: ключи со значением
    # ``None`` (макросы, RDA, bmr — стёрты при замене расчёта) не уезжают,
    # иначе потребитель прочитал бы «белок: null» как «белок неизвестен»
    # там, где белка просто нет.
    if profile.targets_source == NutritionProfile.TargetsSource.USER_ENTERED:
        return {k: v for k, v in block.items() if v is not None}
    return block


def serialize_profile(profile: NutritionProfile, external_user_id: str) -> dict:
    """Конверт профиля для ручек, пишущих в него вне ``upsert_profile``."""
    return _serialize(profile, external_user_id, exists=True)


def _serialize(
    profile: NutritionProfile, external_user_id: str, *, exists: bool,
) -> dict:
    return {
        "external_user_id": external_user_id,
        "exists": exists,
        "gender": profile.gender or None,
        "age": profile.age,
        "height_cm": profile.height_cm,
        "weight_kg": profile.weight_kg,
        "weight_range": profile.weight_range or None,
        "activity_coefficient": profile.activity_coefficient,
        "goal": profile.goal or None,
        "pace": profile.pace or None,
        "diet_preference": profile.diet_preference or "none",
        "norms": _norms_block(profile),
        "health_flags": profile.health_flags or {},
        "goal_overridden_by": profile.goal_overridden_by or None,
        "bmi_warning_overridden_at": _strip_microseconds(
            profile.bmi_warning_overridden_at,
        ),
        "overrides_applied": profile.last_overrides_applied or [],
        # DRF-1339: список входов, которые ПОДСТАВЛЯЛИСЬ вместо ответов
        # человека. Подстановки больше нет — недостающий вход отменяет
        # расчёт, — поэтому список всегда пуст, а имя отказа приезжает в
        # ``overrides_applied`` выше как ``insufficient_inputs``.
        #
        # Ключ оставлен: его читают потребители, а исчезновение
        # означало бы для них «подстановок не было», что для строк,
        # посчитанных ДО этой правки, неправда. Пустой список честнее:
        # с этой правки подстановок действительно нет ни одной.
        "assumed_inputs": [],
        # Происхождение ориентира (DRF-1623 N-d). Уезжает наружу, потому
        # что §92 п.5 адресован ПОКАЗУ: «уже рассчитанный ориентир нельзя
        # продолжать показывать как актуальный без его происхождения».
        # Значит показывающая сторона обязана его получить — иначе
        # правило неисполнимо в принципе, а не просто не исполнено.
        #
        # Снимок входов уезжает ВМЕСТЕ с происхождением — владельцу данных.
        #
        # До 11.09.2026 снимок наружу не уходил: он был нужен для
        # воспроизводимости на нашей стороне, а отдать его значило бы
        # разослать параметры тела туда, где они не нужны. Решение
        # владельца 11.09.2026 §5.1 требует обратного для ОДНОГО адресата:
        # «методика и использованные данные показываются человеку».
        # Ориентир без слов «от чего посчитан» — число, которому человек
        # должен верить на слово; §5.1 запрещает так показывать.
        #
        # Адресат — сам человек: эта ручка отвечает по
        # ``X-External-User-ID``, то есть о профиле того, кто спрашивает,
        # и показывающая сторона (бот в личном диалоге) рисует это ему
        # же. Салону и мастеру снимок не уходит — у них этой ручки нет
        # (§6: «пищевой дневник не передаётся салону или мастеру»; те же
        # границы для параметров тела). ``health_flags`` в снимке нет по
        # построению (спецкатегория 152-ФЗ, см. ``SNAPSHOT_INPUTS``).
        #
        # Пустой словарь, когда расчёта не было или он снят: снимок
        # стирается вместе с ориентиром (``_recompute_and_persist``,
        # ``clear_targets_without_provenance``), и здесь нечего прятать —
        # и нечего выдумывать.
        "targets_provenance": {
            "source": profile.targets_source,
            "method_versions": profile.targets_method_versions or {},
            "computed_at": _strip_microseconds(profile.targets_computed_at),
            # §5.1: подтверждение — часть происхождения. ``None`` при
            # ``ayla_proposed`` (ещё не подтверждено), при ``none`` и у
            # строк, поставленных до введения подтверждения.
            "confirmed_at": _strip_microseconds(profile.targets_confirmed_at),
            "input_snapshot": dict(profile.targets_input_snapshot or {}),
        },
        "disclaimer_acked": profile.disclaimer_acked,
        "onboarded_at": _strip_microseconds(profile.onboarded_at),
        "first_food_logged_at": _strip_microseconds(profile.first_food_logged_at),
        "weekly_summary_unlocked_at": _strip_microseconds(
            profile.weekly_summary_unlocked_at,
        ),
        "created_at": _strip_microseconds(profile.created_at),
        "updated_at": _strip_microseconds(profile.updated_at),
    }


def _strip_microseconds(value):
    """Render datetimes as ms-precision ISO strings.

    Both the live POST response and the idempotency-cache replay must
    render datetimes identically. DRF's default JSON encoder keeps
    millisecond precision (``.NNNZ``); DjangoJSONEncoder keeps full
    microseconds — the cache roundtrip would diverge from the live
    response without this. Returning a string from ``_serialize`` makes
    DRF pass it through verbatim, eliminating the format mismatch.
    """
    if value is None:
        return None
    ms = value.microsecond // 1000
    base = value.replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%S")
    suffix = f".{ms:03d}Z" if value.tzinfo else f".{ms:03d}"
    return base + suffix


# ---------------------------------------------------------------------------
# Подтверждение предложенного ориентира (§5.1, 11.09.2026)
# ---------------------------------------------------------------------------


class NothingToConfirm(Exception):
    """Подтверждать нечего: ориентир не в состоянии ``ayla_proposed``.

    Несёт текущий источник, чтобы вызывающий отличил «ещё не считали»
    (``none``) от «поставлено рукой» (``user_entered``) и от строк без
    происхождения (``unknown_legacy``) — три разных ответа человеку.
    """

    code = "NOTHING_TO_CONFIRM"

    def __init__(self, source: str) -> None:
        self.source = source
        super().__init__(
            f"Подтверждать нечего: ориентир в состоянии {source!r}, "
            "а не 'ayla_proposed'."
        )


def confirm_targets(*, user, external_user_id: str) -> tuple[dict, str]:
    """Человек подтверждает предложенный ориентир: ``ayla_proposed`` → ``ayla_calculated``.

    Возвращает ``(ответ профиля, исход)``, где исход — ``"confirmed"``
    либо ``"already_confirmed"``. Второй — на повтор подтверждения уже
    подтверждённого: это не ошибка (кнопку нажали дважды), но и не новое
    событие — ``targets_confirmed_at`` не переписывается, иначе повтор
    выглядел бы как более позднее решение.

    Подтверждается ТО, что предложено: значения не пересчитываются, снимок
    и версия остаются теми же — человек подтверждает число, которое видел,
    а не то, что получилось бы сейчас. Если входы изменились, пересчёт
    уже перевёл строку обратно в ``ayla_proposed`` (см.
    ``_recompute_and_persist``), и подтверждать нужно заново.

    ``none`` / ``unknown_legacy`` / ``user_entered`` — отказ с именем
    (:class:`NothingToConfirm`): подтвердить можно только предложение.
    """
    with transaction.atomic():
        profile = (
            NutritionProfile.objects.select_for_update().filter(user=user).first()
        )
        if profile is None:
            raise NothingToConfirm(NutritionProfile.TargetsSource.NONE)
        source = profile.targets_source
        if source == NutritionProfile.TargetsSource.AYLA_CALCULATED:
            return _serialize(profile, external_user_id, exists=True), "already_confirmed"
        if source != NutritionProfile.TargetsSource.AYLA_PROPOSED:
            raise NothingToConfirm(source)
        profile.targets_source = NutritionProfile.TargetsSource.AYLA_CALCULATED
        profile.targets_confirmed_at = datetime.now(dt_tz.utc)
        profile.save(update_fields=["targets_source", "targets_confirmed_at", "updated_at"])
    return _serialize(profile, external_user_id, exists=True), "confirmed"
