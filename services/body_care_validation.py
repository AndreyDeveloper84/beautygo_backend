"""Состояние валидации предложения body-care — Body Care CAT-6 (контракт §7).

Контракт §7: это **отдельный инженерный enum**, не ``SafetyState`` и не новое
аварийное состояние безопасности (§24.9). Пять состояний::

    incomplete           не хватает существенных фактов конфигурации
    review_required      факты есть, но конфигурацию никто не проверил
                         (или проверили другую её версию)
    ready_for_screening  конфигурация определена и проверена — можно
                         запускать скрининг
    blocked              источники расходятся (факт CONFLICT)
    retired              канон выведен из оборота (CAT-2)

плюс маркер функции — **не** состояние §7 — ``not_subject``: у канона нет
семейства body-care (стрижка, маникюр). Эта проверка к таким услугам не
относится, и читатель (CAT-10) пропускает их, решая по-прежнему. ``None``
не возвращается никогда: «не посчитали» и «не подлежит» не должны выглядеть
одинаково (§4).

### Как сворачивается — лестница от сильнейшего (решение главного окна, 06.10)

1. семейства нет → ``not_subject``;
2. канон ``retired`` → ``retired``;
3. требования (CAT-5) — ``conflict`` → ``blocked``;
4. требования — ``incomplete`` → ``incomplete``;
5. требования ``complete``, но действующего ревью нет → ``review_required``;
6. требования ``complete`` и ревью той же версии конфигурации →
   ``ready_for_screening``.

Неопределённость сворачивается вниз: умолчание — никогда не READY.

### Что вычисляется, а что хранится

Состояние вычисляется при каждом чтении: хранимое состояние устаревало бы
при любом изменении факта. Хранится только **решение** — ревью
конфигурации на ``SalonService`` (``config_reviewed_*``) с провенансом и
версией, которую проверили.

Входов §7A (лицензия, адрес, квалификация → ``blocked``) пока нет — они
подключатся в фазе §7A.

### Чем READY не является (владелец, 06.10)

``ready_for_screening`` — готовность **конфигурации** предложения, а не
допуск клиента: юридические условия (§7A) и клиентские ограничения
(скрининг) проверяются отдельно. Это предусловие рекомендуемости (CAT-10),
слой предложения; слой клиента — скрининг.

Ревью проверяет полноту конфигурации и соответствие уже утверждённому
протоколу; ревьюер — владелец или явно уполномоченный куратор того салона.
Кто вправе ревьюить, база не проверяет — это слой прав.

### Пробел: отзыв и замена источника (DRF-2828)

Ревью устаревает, только когда меняется ``configuration_version``. Правка
факта, в том числе замена его источника, версию сама не поднимает, а отзыв
источника (реестра источников нет) базе не виден вовсе. До DRF-2828 это
дисциплина пишущего, а не гарантия.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from services.body_care_requirements import (
    COMPLETE as REQ_COMPLETE,
    CONFLICT as REQ_CONFLICT,
    REQUIREMENTS,
    evaluate,
)
from services.models import OfferingConfigFact, SalonService, ServiceTemplate

NOT_SUBJECT = "not_subject"
INCOMPLETE = "incomplete"
REVIEW_REQUIRED = "review_required"
READY_FOR_SCREENING = "ready_for_screening"
BLOCKED = "blocked"
RETIRED = "retired"

#: Ровно пять состояний §7 (v0.2). ``NOT_SUBJECT`` в их число не входит.
VALIDATION_STATES = (INCOMPLETE, REVIEW_REQUIRED, READY_FOR_SCREENING, BLOCKED, RETIRED)

_RETIRED_LIFECYCLE = ServiceTemplate.Lifecycle.RETIRED


def _state(row: dict, facts: dict[str, str]) -> str:
    family = row["template__service_family"]
    if not family:
        return NOT_SUBJECT
    if row["template__lifecycle"] == _RETIRED_LIFECYCLE:
        return RETIRED
    check = evaluate(family, facts, REQUIREMENTS)
    if check.state == REQ_CONFLICT:
        return BLOCKED
    if check.state != REQ_COMPLETE:
        return INCOMPLETE
    version = row["configuration_version"]
    reviewed = row["config_reviewed_version"]
    if version and reviewed == version:
        return READY_FOR_SCREENING
    return REVIEW_REQUIRED


def validation_states(salon_service_ids: Iterable[object]) -> dict[object, str]:
    """``{pk предложения: состояние}`` для пула — два запроса, без N+1.

    Точка входа для CAT-10 (допустимость к рекомендации). Значения —
    одно из ``VALIDATION_STATES`` или ``NOT_SUBJECT``. Отсутствующий в
    базе ``pk`` в ответ не попадает.
    """
    ids = list(salon_service_ids)
    rows = {
        row["pk"]: row
        for row in SalonService.objects.filter(pk__in=ids).values(
            "pk",
            "template__service_family",
            "template__lifecycle",
            "configuration_version",
            "config_reviewed_version",
        )
    }
    facts: dict[object, dict[str, str]] = defaultdict(dict)
    for sid, field_name, state in OfferingConfigFact.objects.filter(
        salon_service_id__in=rows
    ).values_list("salon_service_id", "field", "state"):
        facts[sid][field_name] = state
    return {pk: _state(row, facts.get(pk, {})) for pk, row in rows.items()}


def validation_state(salon_service: SalonService) -> str:
    """Состояние одного предложения."""
    return validation_states([salon_service.pk])[salon_service.pk]
