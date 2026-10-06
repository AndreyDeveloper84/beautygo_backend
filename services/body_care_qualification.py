"""Состояние квалификации пары «мастер × канон» — выводится (Body Care §7A-4).

Контракт v0.2 §7A.4: медицинскую услугу оказывает мастер с проверенной
квалификацией нужного класса. Квалификация — факт мастера
(``users.PractitionerQualification``), требование — факт канона
(``ServiceTemplate.required_practitioner_class``, §7A-0, подтверждает
клиника). Одну услугу салона делают разные мастера, поэтому ответ — по паре
``(specialist_id, template_id)``: той, которой мастер совпал (CAT-10-ext,
гейт записи).

### Состояния (решения главного окна, 06.10)

``not_required``             требования нет, и класс не медицинский;
``requirement_unconfirmed``  класс медицинский, а требование клиникой не
                             подтверждено — не открывает (Q2b);
``not_verified``             у мастера нет проверенной квалификации ровно
                             требуемого класса — код
                             ``PRACTITIONER_QUALIFICATION_NOT_VERIFIED``;
``verified``                 есть.

* Подтверждённое требование проверяется **независимо от класса** (Q2a):
  клиника сказала, что нужна квалификация, — исполняем и для немедицинского.
* **Точное совпадение** класса (Q1): покрывает ли врач требование
  «медсестра» — иерархия клиники (D-2), её здесь нет.
* ``protocol_specific`` — всегда ``not_verified`` (Q3): у канона нет ссылки
  на протокол, сверять протокол мастера не с чем; что такое протокол —
  решает клиника (D-2). Ссылку на канон здесь не вводим.

Читаются только подтверждённые поля §7A-0, кандидат §7A-1 — никогда.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from services.models import ServiceTemplate
from users.models import PractitionerQualification

NOT_REQUIRED = "not_required"
REQUIREMENT_UNCONFIRMED = "requirement_unconfirmed"
NOT_VERIFIED = "not_verified"
VERIFIED = "verified"

QUALIFICATION_STATES = (NOT_REQUIRED, REQUIREMENT_UNCONFIRMED, NOT_VERIFIED, VERIFIED)

#: Код §21 (бот-реестр) для отказного состояния.
CODES = {NOT_VERIFIED: "PRACTITIONER_QUALIFICATION_NOT_VERIFIED"}

LC = ServiceTemplate.LegalServiceClass
PC = ServiceTemplate.PractitionerClass

MEDICAL_CLASSES = frozenset({LC.MEDICAL_COSMETOLOGY.value, LC.MEDICAL_OTHER.value})

PROTOCOL_REASON = "у канона нет ссылки на протокол — сверять протокол мастера не с чем (D-2)"


@dataclass(frozen=True)
class QualificationState:
    state: str
    reason: str = ""


def _state(template: dict, specialist_id, verified: set) -> QualificationState:
    required = template["required_practitioner_class"]
    if required:
        if required == PC.PROTOCOL_SPECIFIC:
            return QualificationState(NOT_VERIFIED, PROTOCOL_REASON)
        if (specialist_id, required) in verified:
            return QualificationState(VERIFIED)
        return QualificationState(
            NOT_VERIFIED, f"нет проверенной квалификации класса {required}"
        )
    if template["legal_service_class"] in MEDICAL_CLASSES:
        return QualificationState(
            REQUIREMENT_UNCONFIRMED, "класс медицинский, требование к квалификации не подтверждено"
        )
    return QualificationState(NOT_REQUIRED)


def qualification_states(
    pairs: Iterable[tuple[object, object]],
) -> dict[tuple[object, object], QualificationState]:
    """``{(specialist_id, template_id): состояние}`` — два запроса на пул.

    Пара с неизвестным каноном в ответ не попадает.
    """
    pairs = list(pairs)
    templates = {
        t["pk"]: t
        for t in ServiceTemplate.objects.filter(pk__in={tid for _, tid in pairs}).values(
            "pk", "legal_service_class", "required_practitioner_class"
        )
    }
    verified = set(
        PractitionerQualification.objects.filter(
            specialist_id__in={sid for sid, _ in pairs}, verified_by__isnull=False
        ).values_list("specialist_id", "practitioner_class")
    )
    return {
        (sid, tid): _state(templates[tid], sid, verified)
        for sid, tid in pairs
        if tid in templates
    }


def qualification_state(specialist_id, template_id) -> QualificationState:
    """Состояние одной пары."""
    return qualification_states([(specialist_id, template_id)])[(specialist_id, template_id)]
