"""Закрытый реестр `reason_codes` — контракт §7.2.

Закрытый значит закрытый: наружу уходят члены этого перечисления и ничего
больше. Аналитика ключуется по кодам, никогда по текстам; добавление кода —
изменение :data:`REGISTRY_VERSION`.

Namespace разведён с треком A (`DECISION_READINESS_ENGINE_v1.0.md`):
там `STATE_`, `SAFETY_`, `BLOCK_`, `REQ_CONTEXT_`, `ASK_`, `EVID_`,
`DELEG_`, `MEASURE_`, `SLOT_`; здесь `SCOPE_`, `ELIG_`, `MATCH_`, `EXEC_`,
`CONTEXT_`, `QUALITY_`, `TIE_`. Пересечений нет — проверяется тестом.

**Кода, означающего «рейтинг как основание рекомендации», в реестре нет
и не будет** (§7.2). `QUALITY_RATING_SUBSTANTIATED` разрешён только рядом
с кодом семейства `MATCH_` — иначе получилось бы «рекомендуем, потому что
хороший рейтинг», а это ровно то утверждение, из-за которого человеку
показали «Рейтинг 4.9» при нуле отзывов (аудит §3.7).
"""
from __future__ import annotations

from enum import StrEnum


#: Версия реестра. Любое добавление, удаление или изменение смысла кода —
#: новая версия; она уезжает в `policy_versions` ответа, иначе решение
#: невоспроизводимо задним числом (контракт §6.3).
REGISTRY_VERSION = "1.0.0"


class ReasonCode(StrEnum):
    """Все коды системы. Значение равно имени — коды видны в логах и в grep."""

    # -- S0: границы поиска -------------------------------------------------
    SCOPE_WITHIN_CITY = "SCOPE_WITHIN_CITY"
    SCOPE_WITHIN_TENANT = "SCOPE_WITHIN_TENANT"
    SCOPE_WITHIN_REQUESTED_AREA = "SCOPE_WITHIN_REQUESTED_AREA"
    SCOPE_GEO_UNKNOWN_EXCLUDED = "SCOPE_GEO_UNKNOWN_EXCLUDED"
    SCOPE_CROSS_TENANT_ALLOWED = "SCOPE_CROSS_TENANT_ALLOWED"
    SCOPE_EXCLUDED_OUT_OF_CITY = "SCOPE_EXCLUDED_OUT_OF_CITY"
    SCOPE_EXCLUDED_OUT_OF_TENANT = "SCOPE_EXCLUDED_OUT_OF_TENANT"
    SCOPE_EXCLUDED_OUT_OF_AREA = "SCOPE_EXCLUDED_OUT_OF_AREA"

    # -- S1: жёсткая допустимость ------------------------------------------
    ELIG_CAPABILITY_VERIFIED = "ELIG_CAPABILITY_VERIFIED"
    ELIG_ACTIVE_OFFER = "ELIG_ACTIVE_OFFER"
    ELIG_WITHIN_STATED_BUDGET = "ELIG_WITHIN_STATED_BUDGET"
    ELIG_SAFETY_CLEARED = "ELIG_SAFETY_CLEARED"
    ELIG_EXCLUDED_INACTIVE = "ELIG_EXCLUDED_INACTIVE"
    ELIG_EXCLUDED_NOT_CAPABLE = "ELIG_EXCLUDED_NOT_CAPABLE"
    ELIG_EXCLUDED_SAFETY = "ELIG_EXCLUDED_SAFETY"
    ELIG_EXCLUDED_BUDGET = "ELIG_EXCLUDED_BUDGET"
    ELIG_EXCLUDED_NOT_RECOMMENDABLE = "ELIG_EXCLUDED_NOT_RECOMMENDABLE"

    # -- S2: соответствие нужде --------------------------------------------
    MATCH_SERVICE_EXACT = "MATCH_SERVICE_EXACT"
    MATCH_SERVICE_PARTIAL = "MATCH_SERVICE_PARTIAL"
    MATCH_CAPABILITY_ONLY = "MATCH_CAPABILITY_ONLY"
    MATCH_GOAL_CATEGORY = "MATCH_GOAL_CATEGORY"
    MATCH_UNDETERMINED = "MATCH_UNDETERMINED"

    # -- S3: транзакционная пригодность ------------------------------------
    EXEC_BOOKABLE = "EXEC_BOOKABLE"
    EXEC_SLOT_CONFIRMED_IN_WINDOW = "EXEC_SLOT_CONFIRMED_IN_WINDOW"
    EXEC_SCHEDULE_UNCONFIRMED = "EXEC_SCHEDULE_UNCONFIRMED"
    EXEC_PRICE_INTENT_APPLIED = "EXEC_PRICE_INTENT_APPLIED"
    EXEC_PRICE_UNKNOWN = "EXEC_PRICE_UNKNOWN"

    # -- S4: контекстная персонализация ------------------------------------
    CONTEXT_PRIOR_COMPLETED_VISIT = "CONTEXT_PRIOR_COMPLETED_VISIT"
    CONTEXT_PRIOR_SAME_CATEGORY = "CONTEXT_PRIOR_SAME_CATEGORY"
    CONTEXT_NOT_APPLICABLE = "CONTEXT_NOT_APPLICABLE"

    # -- S5: качество -------------------------------------------------------
    QUALITY_RATING_SUBSTANTIATED = "QUALITY_RATING_SUBSTANTIATED"
    QUALITY_RATING_UNSUBSTANTIATED_IGNORED = "QUALITY_RATING_UNSUBSTANTIATED_IGNORED"
    QUALITY_NO_EVIDENCE = "QUALITY_NO_EVIDENCE"

    # -- S6: ничьи ----------------------------------------------------------
    TIE_ROTATION_APPLIED = "TIE_ROTATION_APPLIED"
    TIE_TIER_SHARED = "TIE_TIER_SHARED"


#: Префиксы, которыми владеет настоящий контракт. Трек A владеет другими;
#: пересечение означало бы, что два документа независимо версионируют одно имя.
OWNED_PREFIXES = ("SCOPE_", "ELIG_", "MATCH_", "EXEC_", "CONTEXT_", "QUALITY_", "TIE_")

#: Коды, которыми S0/S1 объясняют ИСКЛЮЧЕНИЕ кандидата. Только эти два
#: семейства: S2–S6 не исключают, они упорядочивают (контракт §4.4).
EXCLUSION_CODES = frozenset({
    ReasonCode.SCOPE_GEO_UNKNOWN_EXCLUDED,
    ReasonCode.SCOPE_EXCLUDED_OUT_OF_CITY,
    ReasonCode.SCOPE_EXCLUDED_OUT_OF_TENANT,
    ReasonCode.SCOPE_EXCLUDED_OUT_OF_AREA,
    ReasonCode.ELIG_EXCLUDED_INACTIVE,
    ReasonCode.ELIG_EXCLUDED_NOT_CAPABLE,
    ReasonCode.ELIG_EXCLUDED_SAFETY,
    ReasonCode.ELIG_EXCLUDED_BUDGET,
    ReasonCode.ELIG_EXCLUDED_NOT_RECOMMENDABLE,
})

#: Семейство `MATCH_`, кроме `MATCH_UNDETERMINED`: «нужда чем-то закрыта».
#: `MATCH_UNDETERMINED` сюда не входит намеренно — это признание, что
#: соответствие не вычислялось, а не свидетельство соответствия.
_SUBSTANTIVE_MATCH = frozenset({
    ReasonCode.MATCH_SERVICE_EXACT,
    ReasonCode.MATCH_SERVICE_PARTIAL,
    ReasonCode.MATCH_CAPABILITY_ONLY,
    ReasonCode.MATCH_GOAL_CATEGORY,
})


class ReasonCodeInvariantError(ValueError):
    """Нарушен инвариант реестра §7.2. Программная ошибка, не пользовательская."""


def validate_candidate_codes(codes: frozenset[ReasonCode]) -> tuple[ReasonCode, ...]:
    """Проверить инварианты §7.2 и вернуть коды в лексикографическом порядке.

    Три инварианта, каждый — из контракта, ни одного «на вкус»:

    1. **Хотя бы один код.** Кандидат без объяснения — это строка, про
       которую нельзя сказать, почему она здесь; такие мы и убираем.
    2. **Ни одного кода исключения** у кандидата, попавшего в выдачу.
       Исключение живёт в `excluded[]`, и смешение двух списков сделало бы
       «почему его нет» невыразимым.
    3. **`QUALITY_RATING_SUBSTANTIATED` только рядом с `MATCH_`.** Рейтинг
       не бывает причиной рекомендации сам по себе: он вторичное
       свидетельство после соответствия нужде (канон §9.1, §29.4).

    Порядок лексикографический — ответ детерминирован, и аналитика не
    считает два одинаковых набора разными из-за перестановки.
    """
    if not codes:
        raise ReasonCodeInvariantError("кандидат обязан нести хотя бы один reason_code (§7.2)")
    forbidden = codes & EXCLUSION_CODES
    if forbidden:
        raise ReasonCodeInvariantError(
            f"код исключения у кандидата в выдаче: {sorted(forbidden)} — исключения живут в excluded[] (§4.4)"
        )
    if ReasonCode.QUALITY_RATING_SUBSTANTIATED in codes and not (codes & _SUBSTANTIVE_MATCH):
        raise ReasonCodeInvariantError(
            "QUALITY_RATING_SUBSTANTIATED без кода семейства MATCH_ — рейтинг не является "
            "основанием рекомендации сам по себе (§7.2)"
        )
    return tuple(sorted(codes))
