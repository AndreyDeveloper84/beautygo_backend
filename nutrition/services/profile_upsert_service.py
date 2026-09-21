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
from nutrition.services.targets_state import (
    KIND_CALORIES,
    KIND_FIELDS,
    KIND_SOURCE_FIELD,
    KIND_STAMP_FIELD,
    effective_kind_source,
    kind_source,
    overall_source,
)
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
            _refuse_recompute(profile, payload)
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


#: Входы расчёта: запрос, трогающий любой из них, делает лежащее рядом
#: предложение устаревшим (оно посчитано от прежних).
_CALCULATION_INPUTS: frozenset[str] = frozenset({
    "gender", "age", "height_cm", "weight_kg", "weight_range",
    "activity_coefficient", "goal", "pace", "health_flags", "_skipped_fields",
})


def _recompute_and_persist(profile: NutritionProfile) -> None:
    Source = NutritionProfile.TargetsSource
    # DRF-2219 (§63): пол и цель — только названные человеком. Не назван —
    # расчёт отказывает с именем поля (``insufficient_inputs``), а не
    # считает по женской формуле на «поддержание». Темп и активность пока с
    # умолчаниями — ждут решения владельца (вопрос 59).
    norms = compute_norms(ProfileInputs(
        gender=profile.gender or "",
        age=profile.age,
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
        activity_coefficient=profile.activity_coefficient or DEFAULT_ACTIVITY,
        goal=profile.goal or "",
        pace=profile.pace or "moderate",
        health_flags=profile.health_flags or {},
    ))

    # ── Действующее не заменяется (DRF-2192, DRF-2193; §63, 21.09.2026) ──
    #
    # До этой правки пересчёт писал новые числа ПОВЕРХ действующих, ставил
    # ``ayla_proposed`` всем видам и стирал ``confirmed_at``: между «вес
    # изменился» и «человек подтвердил» ориентира не было вовсе, а ручная
    # норма пропадала от простого «мой вес 61». Решение — ПО ВИДАМ
    # (DRF-1929):
    #
    # * вид НЕ действует (``none`` / ``ayla_proposed`` / ``unknown_legacy``)
    #   — пересчёт ложится на место, как раньше: предложение до
    #   подтверждения (§5.1) или «ориентира нет» при отказе;
    # * вид действует по расчёту (``ayla_calculated``) и расчёт СОСТОЯЛСЯ —
    #   числа, подпись и ``confirmed_at`` остаются, новое предложение
    #   ложится в ``pending_proposal``; ``confirm_targets`` его забирает;
    # * вид задан рукой (``user_entered``) — не трогается никогда и
    #   предложения рядом не получает (вариант (i) GO): предлагать расчёт
    #   поверх ориентира, который человек назвал сам — часто со слов
    #   врача, — непрошеный совет, от которого ручной режим и существует.
    #
    # ОТКАЗ расчёта — другое дело. Он значит, что методика больше не стоит
    # за числом: беременность, кормление, РПП, несовершеннолетие, жёсткий
    # пол калорий, нехватка входа. Расчётный вид при отказе гаснет, как и
    # до этой правки: §63 говорит о новом ВЕСЕ, а не о том, чтобы держать
    # дефицит для человека, только что назвавшего беременность. Ручной вид
    # — число человека, а не расчёт, — остаётся и здесь.
    manual_kept = [
        k for k in KIND_FIELDS if effective_kind_source(profile, k) == Source.USER_ENTERED
    ]
    calculated_kept = (
        [k for k in KIND_FIELDS if effective_kind_source(profile, k) == Source.AYLA_CALCULATED]
        if norms.computed
        else []
    )
    kept = manual_kept + calculated_kept
    in_place = [k for k in KIND_FIELDS if k not in kept]

    for kind in in_place:
        for name in KIND_FIELDS[kind]:
            setattr(profile, name, getattr(norms, name))
        setattr(
            profile,
            KIND_SOURCE_FIELD[kind],
            Source.AYLA_PROPOSED if norms.computed else Source.NONE,
        )
        setattr(profile, KIND_STAMP_FIELD[kind], None)

    if calculated_kept:
        # Всё, что человек увидит и подтвердит, лежит в предложении целиком:
        # числа, снимок, версии, цель и темп расчёта и его переопределения.
        profile.pending_proposal = {
            "kinds": list(calculated_kept),
            "values": {
                name: getattr(norms, name)
                for kind in calculated_kept
                for name in KIND_FIELDS[kind]
            },
            "input_snapshot": dict(norms.input_snapshot),
            "method_versions": dict(norms.method_versions),
            "computed_at": _strip_microseconds(datetime.now(dt_tz.utc)),
            "goal": norms.goal,
            "pace": norms.pace,
            "goal_overridden_by": norms.goal_overridden_by,
            "overrides_applied": list(norms.overrides_applied),
        }
    else:
        # Прежнее предложение посчитано от прежних входов, а нового рядом
        # нет (всё легло на место, расчёт отказал или вид ручной) — старое
        # не держим: подтверждение применило бы устаревшее.
        profile.pending_proposal = None

    # ── Аудит ────────────────────────────────────────────────────────────
    #
    # Переопределения — про числа, которые легли на место. Если на место
    # не легло ничего, аудит действующего не трогается: переопределения
    # отложенного расчёта едут в предложении. Запись ручного писателя
    # (``user_entered`` с предупреждениями) у ручного вида сохраняется —
    # его обещание «прежние записи не стираются» держится и здесь.
    if in_place:
        manual_audit = [
            e for e in (profile.last_overrides_applied or [])
            if manual_kept and isinstance(e, dict) and e.get("reason") == "user_entered"
        ]
        profile.last_overrides_applied = manual_audit + list(norms.overrides_applied)

    # ── Происхождение расчёта (DRF-1623 N-d) ─────────────────────────────
    #
    # Снимок входов, версии, цель и темп описывают расчёт КАЛОРИЙ — у
    # справочной воды методики нет. Пишутся, когда калории легли на место;
    # когда калории действуют, остаются как лежат (новые ждут в
    # предложении). Происхождение идёт вместе со значением: новое число со
    # старым происхождением выглядит объяснённым. Отказ не выдаётся за
    # расчёт — снимок пуст, версий нет.
    if KIND_CALORIES in in_place:
        profile.goal = norms.goal
        profile.pace = norms.pace
        profile.goal_overridden_by = norms.goal_overridden_by
        if norms.computed:
            profile.targets_method_versions = dict(norms.method_versions)
            profile.targets_input_snapshot = dict(norms.input_snapshot)
            profile.targets_computed_at = datetime.now(dt_tz.utc)
        else:
            profile.targets_method_versions = {}
            profile.targets_input_snapshot = {}
            profile.targets_computed_at = None

    # Общая подпись — выводом из видов, а не «как лежала»: иначе строка с
    # ручной водой и калориями-предложением читалась бы ботом (он читает
    # общую подпись до перехода на ``by_kind``) как действующая целиком.
    profile.targets_source = overall_source(profile)
    if profile.targets_source not in (Source.AYLA_CALCULATED, Source.USER_ENTERED):
        profile.targets_confirmed_at = None


def _refuse_recompute(profile: NutritionProfile, payload: dict[str, Any]) -> None:
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
    # DRF-2192: запрос тронул вход расчёта, а пересчёта не было — лежащее
    # рядом предложение посчитано от прежних входов. Держать его значило
    # бы, что подтверждение вернёт старые цель и темп поверх новых или
    # применит расчёт к человеку, только что назвавшему беременность.
    if profile.pending_proposal and _CALCULATION_INPUTS & set(payload):
        profile.pending_proposal = None
    logger.warning(
        "nutrition.targets.recompute_refused user=%s source=%s",
        profile.user_id,
        profile.targets_source,
    )


def _flip_lifecycle_markers(profile: NutritionProfile, payload: dict) -> None:
    if payload.get("complete") and profile.onboarded_at is None:
        profile.onboarded_at = datetime.now(dt_tz.utc)


#: Источники, при которых число в ``daily_water_ml`` — не выход снятой
#: формулы: назвал человек либо справочник по полу состоявшегося расчёта.
_WATER_SOURCES = (
    NutritionProfile.TargetsSource.USER_ENTERED,
    NutritionProfile.TargetsSource.AYLA_PROPOSED,
    NutritionProfile.TargetsSource.AYLA_CALCULATED,
)


def _fluids_source(profile: NutritionProfile) -> str | None:
    """Происхождение ЖИДКОСТИ (DRF-1929, F1(б)).

    ``NULL`` в колонке бывает только у строк, записанных до этой правки и
    не прошедших миграцию данных; для них спрашивается прежняя общая
    подпись, иначе существующий клиент потерял бы воду на ровном месте.
    Откат назван ЯВНО и исчезает вместе с ``NULL``-ами — он не умолчание,
    а мост ([[targets_state.kind_confirmed]] держит тот же мост для
    остальных видов).
    """
    return profile.fluids_source or profile.targets_source


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
    # ``daily_water_ml`` едет только при ИЗВЕСТНОМ происхождении числа:
    # ``user_entered`` (назвал человек, §5.1) и ``ayla_proposed`` /
    # ``ayla_calculated`` (справочник по полу, раздел 4). У
    # ``unknown_legacy`` в столбце остаток снятой формулы 30 × вес до
    # команды очистки — его отдать значило бы выдать снятую методику за
    # живую; сюда он не проходит по источнику, а не по значению.
    #
    # DRF-1929 (F1(б)): спрашивается происхождение ЖИДКОСТИ, а не набора.
    # Раньше здесь стоял общий ``targets_source``, и именно поэтому ручные
    # калории меняли судьбу воды: набор становился ``user_entered``, и
    # справочная вода уезжала под чужой подписью — либо её приходилось
    # гасить в NULL, чтобы не соврать. Теперь у воды своя подпись.
    if (
        _fluids_source(profile) in _WATER_SOURCES
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
            # DRF-2192: новое предложение, лежащее РЯДОМ с действующим
            # ориентиром до подтверждения; ``None`` — рядом ничего нет.
            # Ключ добавлен, а не заменяет прежние: бот, не знающий его,
            # читает действующее как раньше.
            "pending_proposal": _pending_block(profile),
            # DRF-1929 (F1(б)): происхождение ПО ВИДАМ. Ключ ``by_kind``
            # добавлен рядом, а не вместо ``source``: бот держит СВОЮ
            # копию множества действующих источников
            # (``nutrition_client.py:327``) и читает ``source`` сегодня —
            # снять его здесь значило бы сломать чтение до того, как бот
            # научится по видам (отдельный PR). ``None`` внутри — «по
            # видам не устанавливалось» (строка до DRF-1929), и это НЕ то
            # же самое, что ``none``.
            "by_kind": {
                "calories": {
                    "source": profile.calories_source,
                    "confirmed_at": _strip_microseconds(profile.calories_confirmed_at),
                },
                "fluids": {
                    "source": profile.fluids_source,
                    "confirmed_at": _strip_microseconds(profile.fluids_confirmed_at),
                },
            },
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


def _pending_block(profile: NutritionProfile) -> dict[str, Any] | None:
    """Предложение рядом с действующим — всё, что подтверждение применит.

    Числа — под теми же именами, что в ``norms``; снимок входов — как у
    действующего (§5.1: «методика и использованные данные показываются»).
    Цель, темп и переопределения — тоже: подтверждение применяет их, и
    человек должен видеть, например, что расчёт перевёл «похудеть» в
    «поддержание» по полу BMR, до того как это подтвердит.
    """
    pending = profile.pending_proposal
    if not pending:
        return None
    values = pending.get("values") or {}
    return {
        "kinds": list(pending.get("kinds") or []),
        **{name: values.get(name) for name in values},
        "input_snapshot": dict(pending.get("input_snapshot") or {}),
        "method_versions": dict(pending.get("method_versions") or {}),
        "computed_at": pending.get("computed_at"),
        "goal": pending.get("goal"),
        "pace": pending.get("pace"),
        "goal_overridden_by": pending.get("goal_overridden_by") or None,
        "overrides_applied": list(pending.get("overrides_applied") or []),
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
    """Подтверждать нечего: нет предложения ни на месте, ни рядом.

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
    """Человек подтверждает предложенный ориентир — лежащий на месте
    (``ayla_proposed`` → ``ayla_calculated``) или рядом с действующим
    (``pending_proposal``, DRF-2192).

    Возвращает ``(ответ профиля, исход)``, где исход — ``"confirmed"``
    либо ``"already_confirmed"``. Второй — на повтор подтверждения уже
    подтверждённого: это не ошибка (кнопку нажали дважды), но и не новое
    событие — ``targets_confirmed_at`` не переписывается, иначе повтор
    выглядел бы как более позднее решение.

    Подтверждается ТО, что предложено: значения не пересчитываются, снимок,
    версии, цель и темп берутся из предложения — человек подтверждает
    число, которое видел, а не то, что получилось бы сейчас. Если входы
    изменились после предложения, пересчёт положил новое (или снял
    устаревшее, если пересчёта не было), и подтверждать нужно заново.

    Вид, заданный рукой (``user_entered``), не переподписывается расчётом
    никогда. Подтверждать нечего — отказ с именем (:class:`NothingToConfirm`).
    """
    Source = NutritionProfile.TargetsSource
    outcome: str | None = None
    with transaction.atomic():
        profile = (
            NutritionProfile.objects.select_for_update().filter(user=user).first()
        )
        if profile is None:
            raise NothingToConfirm(Source.NONE)
        source = profile.targets_source
        pending = profile.pending_proposal or None
        # Виды, у которых предложение лежит НА МЕСТЕ (действующего не было).
        proposed_in_place = [
            k for k in KIND_FIELDS if kind_source(profile, k) == Source.AYLA_PROPOSED
        ]
        # Строка до DRF-1929: подписей по видам нет, решает общая.
        legacy_whole = (
            not proposed_in_place
            and all(kind_source(profile, k) is None for k in KIND_FIELDS)
            and source == Source.AYLA_PROPOSED
        )
        # Из лежащего рядом применяется только то, что всё ещё расчёт.
        applied = [
            k for k in (pending or {}).get("kinds", [])
            if effective_kind_source(profile, k) == Source.AYLA_CALCULATED
        ]

        if not (applied or proposed_in_place or legacy_whole):
            if pending:
                # Рядом лежит то, что применить уже некуда (вид стал ручным):
                # убираем, не выдавая уборку за подтверждение.
                profile.pending_proposal = None
                profile.save(update_fields=["pending_proposal", "updated_at"])
            if source == Source.AYLA_CALCULATED:
                outcome = "already_confirmed"
        else:
            now = datetime.now(dt_tz.utc)
            values = (pending or {}).get("values") or {}
            for kind in applied:
                for name in KIND_FIELDS[kind]:
                    setattr(profile, name, values.get(name))
                setattr(profile, KIND_STAMP_FIELD[kind], now)
            if KIND_CALORIES in applied:
                profile.targets_input_snapshot = dict(pending.get("input_snapshot") or {})
                profile.targets_method_versions = dict(pending.get("method_versions") or {})
                profile.targets_computed_at = now
                profile.goal = pending.get("goal") or profile.goal
                profile.pace = pending.get("pace") or profile.pace
                profile.goal_overridden_by = pending.get("goal_overridden_by") or ""
                profile.last_overrides_applied = list(pending.get("overrides_applied") or [])
            profile.pending_proposal = None

            # DRF-1929 (F1(б)): подтверждение — по виду. Вид на
            # ``ayla_proposed`` становится расчётом; ручной не трогается.
            for kind in proposed_in_place:
                setattr(profile, KIND_SOURCE_FIELD[kind], Source.AYLA_CALCULATED)
                setattr(profile, KIND_STAMP_FIELD[kind], now)

            profile.targets_source = (
                Source.AYLA_CALCULATED if legacy_whole else overall_source(profile)
            )
            # Общая отметка — про расчёт. У строки с ручным видом общая
            # подпись ``user_entered``, и её отметку ставил человек сам —
            # подтверждение расчёта её не переписывает.
            if profile.targets_source == Source.AYLA_CALCULATED:
                profile.targets_confirmed_at = now
            profile.save()
            outcome = "confirmed"

    if outcome is None:
        raise NothingToConfirm(source)
    if outcome == "already_confirmed":
        return _serialize(profile, external_user_id, exists=True), outcome
    return _serialize(profile, external_user_id, exists=True), "confirmed"
