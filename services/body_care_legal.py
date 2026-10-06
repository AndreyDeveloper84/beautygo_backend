"""Кандидат юридического класса body-care — §7A-1 (контракт v0.2 §7A.2).

Контракт §7A.2 даёт правило по умолчанию и прямо называет его результат
**кандидатом** («WR_BASE -> NON_MEDICAL_COSMETIC candidate»). Кандидат — это
предложение системы, а не класс: подтверждённый класс
(``ServiceTemplate.legal_service_class``, §7A-0) ставит только человек, с
автором, датой и основанием. Гейт §7A-6 читает только подтверждённое поле.

### Почему вычисляется, а не хранится (главное окно, 06.10)

Кандидат — чистая функция ``service_family``. Хранимая копия устаревала бы
при смене семейства и требовала бы писателя. Вычисляемый кандидат всегда
актуален и **физически** не может попасть в подтверждённые поля: у него нет
ни колонок, ни писателя. Модуль ничего не пишет в базу.

### Правило — ровно §7A.2, без выдуманного

* ``body_wrap``, ``mechanical_scrub`` → кандидат ``non_medical_cosmetic``;
* ``spa_body`` → кандидата нет: «строжайший класс компонента» требует
  моделей компонентов (CAT-7);
* ``acid_care`` → кандидата нет: класс кислоты (AC_*) определяет
  классификатор (CAT-8, D-4). **Не** ``legal_review_required``: без
  классификатора «класс неизвестен» неотличимо от «не проверяли»;
* семейства нет → ``not_subject``: §7A.2 к услуге не относится.

### Оговорка BASE

Контракт говорит ``WR_BASE`` / ``SCR_BASE`` — базовый вариант, — но не
определяет его, а канон BASE не различает: горячее или компрессионное
обёртывание — тоже ``body_wrap``. Решение главного окна (вариант a):
кандидат даётся любому канону семейства, а основание **несёт оговорку**,
которую видит подтверждающий человек. Какие модальности выводят услугу из
BASE — вопрос клинике (D-4).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from services.models import ServiceTemplate

Family = ServiceTemplate.ServiceFamily
LC = ServiceTemplate.LegalServiceClass

SYSTEM_DERIVED = "system_derived"
RULE = "bc-7a2-default"
#: Меняется вместе с любой правкой правила: кандидат, предложенный по
#: правилу без версии, нельзя потом воспроизвести.
RULE_VERSION = "0.1"

NOT_SUBJECT = "not_subject"

BASE_CAVEAT = "BASE каноном не различается: проверьте, нет ли тепла или компрессии"


@dataclass(frozen=True)
class Candidate:
    #: Предложенный класс или ``None`` — кандидата нет (см. ``reason``).
    value: str | None
    reason: str
    basis: str = ""
    provenance: str = SYSTEM_DERIVED
    rule: str = RULE
    rule_version: str = RULE_VERSION
    #: ``False`` — канон вне body-care, §7A.2 к нему не относится.
    subject: bool = True


_BY_FAMILY: dict[str, Candidate] = {
    Family.BODY_WRAP: Candidate(
        LC.NON_MEDICAL_COSMETIC,
        reason="правило по умолчанию §7A.2 для обёртывания",
        basis=f"§7A.2 WR_BASE -> NON_MEDICAL_COSMETIC candidate; {BASE_CAVEAT}",
    ),
    Family.MECHANICAL_SCRUB: Candidate(
        LC.NON_MEDICAL_COSMETIC,
        reason="правило по умолчанию §7A.2 для механического скраба",
        basis=f"§7A.2 SCR_BASE -> NON_MEDICAL_COSMETIC candidate; {BASE_CAVEAT}",
    ),
    Family.SPA_BODY: Candidate(
        None,
        reason="§7A.2: строжайший класс компонента — нужны модели компонентов SPA (CAT-7)",
    ),
    Family.ACID_CARE: Candidate(
        None,
        reason="§7A.2: класс кислоты AC_* — нужен классификатор (CAT-8, D-4)",
    ),
}

_NOT_SUBJECT = Candidate(
    None, reason=f"{NOT_SUBJECT}: у канона нет семейства body-care", subject=False
)


def _candidate(family: str | None) -> Candidate:
    if not family:
        return _NOT_SUBJECT
    return _BY_FAMILY.get(
        family, Candidate(None, reason=f"для семейства {family} правила §7A.2 нет")
    )


def legal_class_candidates(template_ids: Iterable[object]) -> dict[object, Candidate]:
    """``{pk канона: кандидат}`` — один запрос. Неизвестный pk в ответ не попадает."""
    families = ServiceTemplate.objects.filter(pk__in=list(template_ids)).values_list(
        "pk", "service_family"
    )
    return {pk: _candidate(family) for pk, family in families}


def legal_class_candidate(template: ServiceTemplate) -> Candidate:
    """Кандидат для одного канона."""
    return legal_class_candidates([template.pk])[template.pk]
