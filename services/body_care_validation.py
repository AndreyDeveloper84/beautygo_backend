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
6. требования ``complete`` и ревью той же версии конфигурации с тем же
   отпечатком фактов → ``ready_for_screening``.

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

### Когда ревью устаревает

Ревью действует, пока совпадают **оба** признака: проверенная версия равна
``configuration_version`` и отпечаток текущих фактов
(``config_fingerprint``) равен отпечатку, записанному при ревью. Отпечаток
берёт каждую колонку факта, кроме служебного ``id``: правка значения,
состояния, источника (в том числе **замена** источника), провенанса
фиксации, удаление или добавление строки — ревью неактуально без ручного
подъёма версии. «Значимой» считается любая правка: порога никто не
утверждал, умолчание — fail-closed (главное окно, 06.10).

### Пробел: отзыв источника извне (DRF-2828)

**Отзыв** источника без правки фактов (производитель отозвал инструкцию)
базе не виден: реестра источников нет, факты остаются прежними, отпечаток
сходится. Это открытый пробел DRF-2828.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime

from django.utils import timezone

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

#: Колонки факта, входящие в отпечаток, — все, кроме служебного ``id`` и
#: ссылки на предложение. Новая колонка факта должна попасть сюда
#: (узел ``test_the_fingerprint_covers_every_fact_column``).
FINGERPRINT_COLUMNS = (
    "field",
    "state",
    "value",
    "source_ref",
    "source_type",
    "source_version",
    "captured_at",
    "captured_by_id",
    "captured_rule",
    "capture_rule_version",
    "created_at",
    "updated_at",
)


def _canonical(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def config_fingerprint(fact_rows: Iterable[dict]) -> str:
    """SHA-256 набора фактов конфигурации — не зависит от порядка строк.

    ``fact_rows`` — словари с ключами ``FINGERPRINT_COLUMNS``. Пустой набор
    тоже имеет отпечаток: «фактов не было» — это состояние, которое видел
    ревьюер.
    """
    lines = sorted(
        json.dumps(
            [_canonical(row[c]) for c in FINGERPRINT_COLUMNS],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        for row in fact_rows
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _state(row: dict, fact_rows: list[dict]) -> str:
    family = row["template__service_family"]
    if not family:
        return NOT_SUBJECT
    if row["template__lifecycle"] == _RETIRED_LIFECYCLE:
        return RETIRED
    facts = {f["field"]: f["state"] for f in fact_rows}
    check = evaluate(family, facts, REQUIREMENTS)
    if check.state == REQ_CONFLICT:
        return BLOCKED
    if check.state != REQ_COMPLETE:
        return INCOMPLETE
    version = row["configuration_version"]
    if (
        version
        and row["config_reviewed_version"] == version
        and row["config_reviewed_fingerprint"] == config_fingerprint(fact_rows)
    ):
        return READY_FOR_SCREENING
    return REVIEW_REQUIRED


def _fact_rows(salon_service_ids) -> dict[object, list[dict]]:
    facts: dict[object, list[dict]] = defaultdict(list)
    for fact in OfferingConfigFact.objects.filter(salon_service_id__in=salon_service_ids).values(
        "salon_service_id", *FINGERPRINT_COLUMNS
    ):
        facts[fact["salon_service_id"]].append(fact)
    return facts


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
            "config_reviewed_fingerprint",
        )
    }
    facts = _fact_rows(rows)
    return {pk: _state(row, facts.get(pk, [])) for pk, row in rows.items()}


def record_config_review(
    salon_service: SalonService,
    *,
    source_ref: str,
    by=None,
    rule: str = "",
    rule_version: str = "",
    at: datetime | None = None,
) -> None:
    """Записать ревью текущей конфигурации: версию и отпечаток её фактов.

    Единственный путь записи ревью: отпечаток считает сама функция, по
    фактам в базе на момент записи. Кто вправе ревьюить (владелец или
    уполномоченный куратор того салона), здесь не проверяется — это слой
    прав вызывающего. Конфигурацию без версии ревьюить нельзя: ревью не к
    чему привязать.
    """
    salon_service.refresh_from_db(fields=["configuration_version"])
    version = salon_service.configuration_version
    if not version:
        raise ValueError("конфигурация без версии: ревью не к чему привязать")
    fingerprint = config_fingerprint(_fact_rows([salon_service.pk]).get(salon_service.pk, []))
    fields = {
        "config_reviewed_by": by,
        "config_review_rule": rule,
        "config_review_rule_version": rule_version,
        "config_reviewed_at": at or timezone.now(),
        "config_review_source_ref": source_ref,
        "config_reviewed_version": version,
        "config_reviewed_fingerprint": fingerprint,
    }
    SalonService.objects.filter(pk=salon_service.pk).update(**fields)
    for name, value in fields.items():
        setattr(salon_service, name, value)


def validation_state(salon_service: SalonService) -> str:
    """Состояние одного предложения."""
    return validation_states([salon_service.pk])[salon_service.pk]
