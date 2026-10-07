"""Фаза 1 пакета service_family: область по разделам и класс шести канонам.

Модуль лежит в пакете миграций намеренно: логика принадлежит миграции
``0049``, имя на ``_`` загрузчик Django миграцией не считает.

Решение владельца 07.10 (DRF-2866): классификация правдива, допуск считается
отдельно. Поэтому шаг состоит из двух независимых частей.

Область — правилом раздела
--------------------------
Канону с кодом эталонного справочника ставится ``not_body_care``, если его
раздел **бесспорно** вне Body Care. Основание — раздел кода, не название.
Автор — правило с версией: владелец утвердил таблицу разделов, а не каждую
из тысячи строк.

Область получают и рискованные услуги — лазер, инъекции, пилинги лица. Она
для них правдива, и ничего не открывает: ``not_body_care`` пропускает только
проверку Body Care, а закрывает их проверка юридического класса.

Правилом **не** решаются и остаются с неизвестной областью:

* ``1.4`` (лимфодренаж и коррекция фигуры), раздел ``2`` (аппаратная
  коррекция фигуры) — услуги тела вне четырёх семейств контракта;
* раздел ``3`` (SPA, обёртывания) — подлежит Body Care, но семейство ставит
  человек построчно;
* ``17``, ``18``, ``19`` — смешанные по устройству;
* раздел ``20`` (комплексы) — наследуют самое строгое из составляющих;
* каноны без кода.

Юридический класс — шести канонам, человеком
--------------------------------------------
``non_medical_cosmetic`` ставится шести канонам, которые владелец назвал
бесспорными. Отбор по коду справочника: код — логическая идентичность
канона, одна на всех базах. Автор — именная учётка владельца
(``users/0031``), найденная по ключу; исполнитель назван в основании. Тем же
шестерым область ставится не правилом, а тем же автором.

Остальным класс не ставится: его нет, и юридическая проверка их закрывает.

Чего шаг не делает
------------------
* Не переписывает область и класс, которые уже стоят.
* Не трогает семейство, требуемую квалификацию и флаг проверки здоровья.
* Поведение читателей не меняет: оно под флагом
  ``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED``.

Когда шаг падает
----------------
Канон из шестёрки на месте и ждёт класса, а учётки владельца в базе нет или
она выключена. Молча пропустить нельзя: миграция отметилась бы применённой.
Накатка идёт в транзакции, половины не остаётся.

Сколько строк затронуто, шаг печатает числом — без содержимого.
"""
from __future__ import annotations

#: Копии словарей модели на момент миграции; расхождение сторожит узел.
NOT_BODY_CARE = "not_body_care"
NON_MEDICAL_COSMETIC = "non_medical_cosmetic"

RULE = "canonical_section_outside_body_care"
RULE_VERSION = "1"

#: Подразделы раздела 1 (ручной массаж), кроме 1.4.
NOT_BODY_CARE_SUBSECTIONS = frozenset({"1.1", "1.2", "1.3", "1.5", "1.6"})

#: Разделы, целиком лежащие вне Body Care: лицо (4, 5), инъекции (6),
#: удаление волос (7), ногти (8, 9), волосы (10), брови и ресницы (11, 12),
#: макияж (13), перманент и тату (14, 15), солярий (16), продажа косметики
#: и сервисные позиции (21, 22).
NOT_BODY_CARE_SECTIONS = frozenset(
    {"4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "21", "22"}
)

#: Шесть бесспорных канонов пилота — по коду справочника.
NON_MEDICAL_CODES = (
    "1.1.1",  # классический массаж всего тела
    "1.1.4",  # массаж спины
    "1.1.5",  # массаж шейно-воротниковой зоны
    "1.1.14",  # массаж стоп
    "1.5.13",  # пластический массаж лица
    "4.4.1",  # альгинатная маска
)

_EXECUTOR = "исполнил: окно каталога ayla-8d, миграцией"
SCOPE_RULE_SOURCE_REF = (
    f"owner decision 07.10 (пакет service_family): область по разделу справочника; {_EXECUTOR}"
)
OWNER_SOURCE_REF = f"owner decision 07.10 (пакет service_family); {_EXECUTOR}"


class CannotAttribute(RuntimeError):
    """Канон ждёт решения владельца, а записать его авторство нечем."""


def section_is_outside_body_care(code: str | None) -> bool:
    """Бесспорно ли раздел кода лежит вне Body Care."""
    if not code:
        return False
    section, subsection, *_ = code.split(".")
    return section in NOT_BODY_CARE_SECTIONS or f"{section}.{subsection}" in NOT_BODY_CARE_SUBSECTIONS


def classify(template_model, *, owner_id, now) -> dict[str, int]:
    """Поставить класс шести и область по разделам; вернуть числа."""
    six = template_model.objects.filter(canonical_code__in=NON_MEDICAL_CODES)
    waiting = six.filter(legal_service_class__isnull=True)
    if waiting.exists() and not owner_id:
        raise CannotAttribute("DRF-2866: именной учётки владельца нет — автора решения нет")

    # Шестерым область ставит тот же автор, что и класс, — до правила
    # раздела, которое иначе заняло бы её.
    scoped_by_owner = six.filter(body_care_scope__isnull=True, service_family__isnull=True).update(
        body_care_scope=NOT_BODY_CARE,
        scope_confirmed_by_id=owner_id,
        scope_confirmed_rule="",
        scope_rule_version="",
        scope_confirmed_at=now,
        scope_source_ref=OWNER_SOURCE_REF,
    ) if owner_id else 0
    classed = waiting.update(
        legal_service_class=NON_MEDICAL_COSMETIC,
        legal_class_confirmed_by_id=owner_id,
        legal_class_confirmed_at=now,
        legal_class_source_ref=OWNER_SOURCE_REF,
    )

    by_rule = [
        pk
        for pk, code in template_model.objects.filter(
            canonical_code__isnull=False, body_care_scope__isnull=True, service_family__isnull=True
        ).values_list("pk", "canonical_code")
        if section_is_outside_body_care(code)
    ]
    scoped_by_rule = template_model.objects.filter(pk__in=by_rule).update(
        body_care_scope=NOT_BODY_CARE,
        scope_confirmed_by=None,
        scope_confirmed_rule=RULE,
        scope_rule_version=RULE_VERSION,
        scope_confirmed_at=now,
        scope_source_ref=SCOPE_RULE_SOURCE_REF,
    )
    return {
        "classed": classed,
        "scoped_by_owner": scoped_by_owner,
        "scoped_by_rule": scoped_by_rule,
        "left_unknown": template_model.objects.filter(body_care_scope__isnull=True).count(),
    }


def declassify(template_model) -> dict[str, int]:
    """Снять только то, что поставил ``classify``; вернуть числа.

    Отбор по значению и основанию разом: область или класс, которые после
    накатки поменял человек, остаются как есть.
    """
    blank_scope = {
        "body_care_scope": None,
        "scope_confirmed_by": None,
        "scope_confirmed_rule": "",
        "scope_rule_version": "",
        "scope_confirmed_at": None,
        "scope_source_ref": "",
    }
    return {
        "declassed": template_model.objects.filter(
            canonical_code__in=NON_MEDICAL_CODES,
            legal_service_class=NON_MEDICAL_COSMETIC,
            legal_class_source_ref=OWNER_SOURCE_REF,
        ).update(
            legal_service_class=None,
            legal_class_confirmed_by=None,
            legal_class_confirmed_at=None,
            legal_class_source_ref="",
        ),
        "unscoped": template_model.objects.filter(
            body_care_scope=NOT_BODY_CARE,
            scope_source_ref__in=[OWNER_SOURCE_REF, SCOPE_RULE_SOURCE_REF],
        ).update(**blank_scope),
    }
