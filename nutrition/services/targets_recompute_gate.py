"""Пересчёт ориентиров — только с основанием (§103, срез N-b).

### Третье окно

Команда ``clear_targets_without_provenance`` стирает ориентиры без
происхождения (§103), команда ``purge_unconsented_body_parameters``
стирает входы (§120). Между их запусками входы ещё лежат в строке, а
``upsert_profile`` до этой правки пересчитывал ориентиры на КАЖДОМ POST —
в том числе на ``{"diet_preference": "vegetarian"}``, теле без параметров
тела, которое сторож согласия (#324) пропускает по замыслу: «отказ не
закрывает дневник» (§92).

Значит любой такой POST ВОСКРЕСИЛ бы ориентиры от входов, собранных без
согласия, и — хуже — проставил бы им ``ayla_calculated``: отмыл бы
``unknown_legacy`` в «посчитано Ayla». Это окно закрывается сторожем, а
не очерёдностью запусков: очерёдность живёт в теле PR, сторож — в коде.

### Три сценария

(а) Запрос несёт утверждение о согласии в форме #324 — ``consent.type ==
PERSONAL_CALCULATION`` и непустая строка ``document_version``. Человек
только что назвал основание; считать можно.

(б) ``profile.targets_source`` ∈ {``AYLA_CALCULATED``, ``AYLA_PROPOSED``}. Расчёт уже состоялся
с утверждением, и пересчёт от тех же входов — например, при смене
``health_flags`` — оставляет происхождение тем же. Требовать утверждение
заново значило бы требовать его при каждом изменении флага здоровья,
который живёт под своим согласием.

(в) Всё остальное — ``none``, ``unknown_legacy``, ``user_entered`` без
утверждения — пересчёт запрещён. Ориентиры остаются как лежат: у
``none`` — ``NULL``, у ``unknown_legacy`` — старые числа до команды
очистки. Ни то, ни другое не превращается в ``ayla_calculated``.

### Отказ — громкий, а не молчаливый

Молчаливый отказ дал бы профиль, который выглядит обработанным: POST
вернул 200, а что пересчёта не было — не видно ни в ответе, ни в логе.
Поэтому у отказа есть имя: ``recompute_refused_no_consent`` в
``overrides_applied`` (существующее поле ответа, новый ключ контракта не
нужен) и одна строка ``logger.warning``. Запись отказа НЕ выдаётся за
расчёт: ``targets_source`` остаётся прежним.

### Предел этого сторожа — назван, а не подразумевается

**Отзыв согласия сюда не доезжает.** Сценарий (б) разрешает пересчёт по
``ayla_calculated`` бессрочно: одно историческое утверждение лицензирует
все последующие пересчёты. Человек отзывает ``personal_calculation`` в
боте — реестр согласий живёт там, и каталог об этом не узнаёт ПО
ЗАМЫСЛУ (см. ``personal_calculation_consent``: каталог требует
утверждения, а не проверяет согласие). ``targets_source`` остаётся
``ayla_calculated``, и каждый следующий POST считает заново. §92 п.4
(«отзыв согласия на расчёт… прекращает пересчёт») этим модулем исполнен
для ПЕРВОГО расчёта, а не для повторных. Чинить здесь нечем: чтобы
каталог узнал об отзыве, бот должен либо перестать слать POST, либо
прислать отзыв, — ни того, ни другого пока не существует. Вопрос
владельцу (реестр §149).

**Открытое поле управляет закрытым исходом.** ``pace`` не закрыт
согласием (#324: «темп сам по себе о теле не сообщает»), патчится и
входит в снимок — а через ``PACE_FACTORS`` меняет число, выведенное из
тела. При ``ayla_calculated`` это штатно: пересчёт есть, снимок
обновляется. При ``user_entered`` темп число не объяснял и не тронет —
тест в ``test_targets_recompute_gate`` это держит. «Не сообщает о
теле» и «не может изменить телесное число» — разные утверждения.

Предикат вынесен в функцию, чтобы тест мог подменить его и доказать,
что без сторожа окно открыто (краснеет), а со сторожем — закрыто.
"""
from __future__ import annotations

from typing import Any

from nutrition.models import NutritionProfile
from nutrition.services.personal_calculation_consent import (
    PERSONAL_CALCULATION,
)

#: Имя отказа в ``overrides_applied``. Одно на все случаи (в): различать
#: ``none`` от ``unknown_legacy`` читатель может по ``targets_source``,
#: который едет рядом в той же записи.
RECOMPUTE_REFUSED_NO_CONSENT = "recompute_refused_no_consent"


def carries_attestation(payload: dict[str, Any]) -> bool:
    """Несёт ли тело утверждение о согласии в форме #324.

    Та же форма, что проверяет ``require_consent``: ``type`` и непустая
    ``document_version``. Не вызывает ``require_consent`` напрямую,
    потому что тот отказывает ТОЛЬКО телу с параметрами тела, а здесь
    вопрос другой: есть ли основание считать, независимо от того, что
    в теле.
    """
    attestation = payload.get("consent")
    if not isinstance(attestation, dict):
        return False
    if attestation.get("type") != PERSONAL_CALCULATION:
        return False
    version = attestation.get("document_version")
    return isinstance(version, str) and bool(version.strip())


def recompute_permitted(profile: NutritionProfile, payload: dict[str, Any]) -> bool:
    """Разрешён ли пересчёт ориентиров для этого профиля этим запросом.

    Сценарии (а), (б), (в) — в докстринге модуля. Fail-closed: всё, что не
    названо разрешённым, запрещено.
    """
    if carries_attestation(payload):
        return True
    # (б) — и для ``ayla_proposed``: основание у предложения то же, что у
    # подтверждённого расчёта (утверждение в запросе, который его
    # породил); пересчёт даёт новое предложение, не подтверждение.
    return profile.targets_source in (
        NutritionProfile.TargetsSource.AYLA_CALCULATED,
        NutritionProfile.TargetsSource.AYLA_PROPOSED,
    )


def refusal_record(profile: NutritionProfile) -> dict[str, Any]:
    """Запись об отказе для ``overrides_applied``."""
    return {
        "reason": RECOMPUTE_REFUSED_NO_CONSENT,
        "targets_source": profile.targets_source,
    }
