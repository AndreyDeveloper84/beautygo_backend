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
3a. §7A-6: класс медицинский, а проверенной лицензии салона нет или канон
   вне её объёма → ``UNVERIFIED_LICENSE_STATE`` (сейчас ``blocked``);
4. требования — ``incomplete`` → ``incomplete``;
4a. §7A-6: юридический класс не подтверждён человеком (``NULL`` или
   ``legal_review_required``) → ``review_required``;
5. требования ``complete``, но действующего ревью нет → ``review_required``;
6. требования ``complete``, класс подтверждён, лицензия (если нужна)
   проверена, ревью той же версии конфигурации с тем же отпечатком фактов →
   ``ready_for_screening``.

Неопределённость сворачивается вниз: умолчание — никогда не READY.

### Что вычисляется, а что хранится

Состояние вычисляется при каждом чтении: хранимое состояние устаревало бы
при любом изменении факта. Хранится только **решение** — ревью
конфигурации на ``SalonService`` (``config_reviewed_*``) с провенансом и
версией, которую проверили.

### Входы §7A (§7A-6, DRF-2842)

Сюда сворачиваются только гейты **уровня предложения**: подтверждённый
юридический класс (§7A-0) и лицензия салона (§7A-2, то же правило
``license_state_of``). Гейты мастера — адрес (§7A-3) и квалификация
(§7A-4) — сюда не входят: у предложения нет одного мастера; их читают
CAT-10-ext и гейт записи. Медицинский класс у канона вне body-care
(пилинги лица) остаётся ``not_subject`` здесь и гейтится отдельно
(DRF-2843): ``not_subject`` по-прежнему значит «весь каталог вне Body Care».

Следствие: пока юридический класс body-care канона не подтверждён
человеком, его предложения не бывают READY.

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
from services.body_care_license import (
    CLASS_UNCONFIRMED as LICENSE_CLASS_UNCONFIRMED,
    NOT_VERIFIED as LICENSE_NOT_VERIFIED,
    SCOPE_MISMATCH as LICENSE_SCOPE_MISMATCH,
    license_state_of,
    verified_coverage,
)
from services.body_care_scope import (
    NOT_SUBJECT as SCOPE_NOT_SUBJECT,
    UNCLASSIFIED as SCOPE_UNCLASSIFIED,
    classification_stamp,
    scope_of,
)
from services.models import OfferingConfigFact, SalonService, ServiceTemplate

NOT_SUBJECT = "not_subject"
#: Маркер, как и ``NOT_SUBJECT``, — не состояние §7: классификация канона
#: неизвестна (``body_care_scope.scope_of``), и проверка **не снята**.
#: Потребитель обязан закрыть строку; литерал — контракт с источником
#: рекомендаций (``users.recommendation_source.config_readiness``).
UNCLASSIFIED = "unclassified"
INCOMPLETE = "incomplete"
REVIEW_REQUIRED = "review_required"
READY_FOR_SCREENING = "ready_for_screening"
BLOCKED = "blocked"
RETIRED = "retired"

#: Ровно пять состояний §7 (v0.2). ``NOT_SUBJECT`` в их число не входит.
VALIDATION_STATES = (INCOMPLETE, REVIEW_REQUIRED, READY_FOR_SCREENING, BLOCKED, RETIRED)

_RETIRED_LIFECYCLE = ServiceTemplate.Lifecycle.RETIRED

#: §7A-6: во что сворачивается медицинский класс без проверенной лицензии
#: салона или вне её объёма. BLOCKED — fixture F-BC-016 и fail-closed
#: (решение главного окна, 06.10; вопрос владельцу A2(3)). Если владелец
#: ответит «REVIEW», меняется эта строка: значение уйдёт ниже INCOMPLETE.
UNVERIFIED_LICENSE_STATE = BLOCKED

_LICENSE_FAILED = frozenset({LICENSE_NOT_VERIFIED, LICENSE_SCOPE_MISMATCH})

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


def config_fingerprint(fact_rows: Iterable[dict], classification: list) -> str:
    """SHA-256 фактов конфигурации и классификации канона.

    ``fact_rows`` — словари с ключами ``FINGERPRINT_COLUMNS``; от порядка
    строк отпечаток не зависит. Пустой набор тоже имеет отпечаток: «фактов
    не было» — это состояние, которое видел ревьюер.

    ``classification`` — ``body_care_scope.classification_stamp``: область,
    семейство и версия канона. Ревью проверяло конфигурацию канона именно
    этой классификации; смена любого из трёх делает его неактуальным.
    Строка классификации идёт первой и отдельно от фактов, поэтому с
    фактом её не спутать.
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
    head = json.dumps(classification, ensure_ascii=False, default=str)
    return hashlib.sha256("\n".join([head, *lines]).encode("utf-8")).hexdigest()


def _classification(row: dict) -> list:
    return classification_stamp(
        scope=row["template__body_care_scope"],
        family=row["template__service_family"],
        canonical_version=row["template__canonical_version"] or "",
    )


def _state(row: dict, fact_rows: list[dict], license_state: str) -> str:
    family = row["template__service_family"]
    scope = scope_of(
        has_canon=row["template_id"] is not None,
        scope=row["template__body_care_scope"],
        family=family,
    )
    if scope == SCOPE_UNCLASSIFIED:
        return UNCLASSIFIED
    if scope == SCOPE_NOT_SUBJECT:
        return NOT_SUBJECT
    if row["template__lifecycle"] == _RETIRED_LIFECYCLE:
        return RETIRED
    facts = {f["field"]: f["state"] for f in fact_rows}
    check = evaluate(family, facts, REQUIREMENTS)
    if check.state == REQ_CONFLICT:
        return BLOCKED
    license_failed = license_state in _LICENSE_FAILED
    if license_failed and UNVERIFIED_LICENSE_STATE == BLOCKED:
        return BLOCKED
    if check.state != REQ_COMPLETE:
        return INCOMPLETE
    if license_failed or license_state == LICENSE_CLASS_UNCONFIRMED:
        return REVIEW_REQUIRED
    version = row["configuration_version"]
    if (
        version
        and row["config_reviewed_version"] == version
        and row["config_reviewed_fingerprint"] == config_fingerprint(fact_rows, _classification(row))
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
    """``{pk предложения: состояние}`` для пула — три запроса, без N+1.

    Точка входа для CAT-10 (допустимость к рекомендации). Значения —
    одно из ``VALIDATION_STATES``, ``NOT_SUBJECT`` или ``UNCLASSIFIED``. Отсутствующий в
    базе ``pk`` в ответ не попадает.
    """
    ids = list(salon_service_ids)
    rows = {
        row["pk"]: row
        for row in SalonService.objects.filter(pk__in=ids).values(
            "pk",
            "tenant_id",
            "template_id",
            "template__body_care_scope",
            "template__service_family",
            "template__canonical_version",
            "template__legal_service_class",
            "template__lifecycle",
            "configuration_version",
            "config_reviewed_version",
            "config_reviewed_fingerprint",
        )
    }
    facts = _fact_rows(rows)
    verified_tenants, covered = verified_coverage({r["tenant_id"] for r in rows.values()})
    return {
        pk: _state(row, facts.get(pk, []), license_state_of(row, verified_tenants, covered))
        for pk, row in rows.items()
    }


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
    canon = (
        SalonService.objects.filter(pk=salon_service.pk)
        .values("template__body_care_scope", "template__service_family", "template__canonical_version")
        .get()
    )
    fingerprint = config_fingerprint(
        _fact_rows([salon_service.pk]).get(salon_service.pk, []), _classification(canon)
    )
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
