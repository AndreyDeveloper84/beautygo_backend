"""Область классификации канона и правило «неизвестное не допускается».

Решение владельца 07.10. До него шесть читателей Body Care понимали
«семейства нет» как «не подлежит»: проверка конфигурации (CAT-6), требования
(CAT-5), кандидат класса (§7A-1), лицензия (§7A-2), адрес (§7A-3) и
квалификация (§7A-4). На пилоте семейства нет ни у одного канона — гейты
были слепы. Правило теперь одно и живёт здесь.

### Классификация правдива, допуск считается отдельно

Область (``ServiceTemplate.body_care_scope``) отвечает на один вопрос:
применяется ли к канону проверка Body Care. ``not_body_care`` пропускает
**только её**. Юридический класс проверяется у любого канона, какой бы ни
была область: ``not_body_care`` не значит «немедицинская».

### Ответы ``scope_of``

``subject``       область ``body_care`` и семейство назначено — проверка
                  Body Care применяется;
``not_subject``   область ``not_body_care``, подтверждена — проверка Body
                  Care не применяется;
``unclassified``  область неизвестна; либо ``body_care`` без семейства
                  («подлежит, семейство не определено»); либо у предложения
                  нет канона. Дефект каталога: услуга не рекомендуется, а
                  клиенту вопрос не задаётся.

### Флаг

``settings.BODY_CARE_UNCLASSIFIED_FAIL_CLOSED``. Выключен — прежнее
поведение: есть семейство — ``subject``, нет — ``not_subject``, а
неподтверждённый класс у канона без семейства ничего не закрывает. Включать
только после классификации данных: при пустой области закрывается всё.
Пока флаг выключен, обход открыт.
"""

from __future__ import annotations

from django.conf import settings

from services.models import ServiceTemplate

SUBJECT = "subject"
NOT_SUBJECT = "not_subject"
UNCLASSIFIED = "unclassified"

SCOPES = (SUBJECT, NOT_SUBJECT, UNCLASSIFIED)

Scope = ServiceTemplate.BodyCareScope
LC = ServiceTemplate.LegalServiceClass


def fail_closed() -> bool:
    """Включено ли правило «неизвестное не допускается»."""
    return bool(getattr(settings, "BODY_CARE_UNCLASSIFIED_FAIL_CLOSED", False))


def scope_of(*, has_canon: bool, scope: str | None, family: str | None) -> str:
    """Применяется ли проверка Body Care — по области и семейству канона.

    ``has_canon=False`` — у предложения нет канона: классифицировать нечего,
    и это тоже «неизвестно», а не «не подлежит».
    """
    if not fail_closed():
        return SUBJECT if family else NOT_SUBJECT
    if not has_canon or scope is None:
        return UNCLASSIFIED
    if scope == Scope.NOT_BODY_CARE:
        return NOT_SUBJECT
    if scope == Scope.BODY_CARE and family:
        return SUBJECT
    # ``body_care`` без семейства и любое незнакомое значение области.
    return UNCLASSIFIED


def class_unknown(legal_class: str | None) -> bool:
    """Класс не подтверждён: не назван или ждёт юридической проверки."""
    return legal_class is None or legal_class == LC.LEGAL_REVIEW_REQUIRED


def legal_checks_waived(*, legal_class: str | None, family: str | None) -> bool:
    """Снимает ли класс канона юридические проверки (лицензия, адрес).

    Снимает только **подтверждённый** немедицинский класс. Неизвестный класс
    и ``legal_review_required`` не снимают ничего — у любого канона и у
    предложения без канона.

    При выключенном флаге действует прежнее правило: канон без семейства и
    без класса тоже считается не требующим проверок.
    """
    if legal_class == LC.NON_MEDICAL_COSMETIC:
        return True
    if fail_closed():
        return False
    return not family and legal_class is None


def classification_stamp(*, scope: str | None, family: str | None, canonical_version: str) -> list:
    """Что из классификации входит в отпечаток ревью конфигурации.

    Ревью проверяло конфигурацию канона **этой** области, семейства и версии:
    смена любого из трёх делает ревью неактуальным.
    """
    return ["classification", scope, family, canonical_version]
