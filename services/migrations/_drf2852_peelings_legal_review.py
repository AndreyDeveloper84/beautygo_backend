"""Четыре канона-пилинга — на юридическую проверку; общее для миграции и теста.

Модуль лежит в пакете миграций намеренно, как ``_drf2742_evidence_kind_demote``:
логика принадлежит миграции ``0047``, имя на ``_`` загрузчик Django
миграцией не считает.

Зачем шаг нужен
---------------
Решение владельца 07.10: пилинги салона пилота до юридической проверки не
предлагаются. У четырёх канонов класс не установлен (``NULL``), а семейства
body-care у них нет — и ``license_state_of`` читает такую пару как
``not_required``: «вне Body Care, проверять нечего». Класс
``legal_review_required`` переводит их в ``class_unconfirmed``.

Админки для класса нет, у стенда нет ни shell, ни ORM — остаётся миграция.

Что шаг делает
--------------
Каждому из четырёх канонов, **названных по pk**, у которого класс ещё не
установлен, ставит ``legal_service_class = legal_review_required`` с
провенансом: кто, когда, основание. Автор — именная учётка владельца
(``users/0031``), найденная по ключу; правилом («первый администратор») он
не выбирается: сказанное человеком не подменяется выводом системы.
Исполнитель назван в основании.

Чего шаг не делает
------------------
* Канон, которому класс уже поставил человек, не трогает — и печатает число.
* Другие каноны не трогает: отбор по четырём pk, а не по слову в имени.
* Требуемую квалификацию (``required_practitioner_class``) не ставит.
* На базе, где этих канонов нет (CI, разработка), ничего не пишет.

Когда шаг падает
----------------
Канон на месте, а записать честно нельзя: учётки владельца в базе нет
(или она выключена), либо под pk лежит не пилинг. Молча пропустить нельзя — миграция
отметилась бы применённой, а каноны остались бы открытыми навсегда. Накатка
идёт в транзакции, так что падение не оставляет половины.

Сколько строк затронуто, шаг печатает числом — без содержимого.
"""
from __future__ import annotations

#: Каноны салона пилота (readback главного окна, 07.10). Имена — только в
#: комментарии: сверяется pk и то, что под ним действительно пилинг.
PEELING_TEMPLATE_IDS = (
    "3a90645d-887a-4c0d-b91a-28ac2cc48dc7",  # Азелаиновый пилинг
    "677ce76b-ec04-44d5-8f94-cceb8bd3723e",  # Миндальный пилинг
    "2fc8c5fd-94bd-4511-8402-e426ee70d9ba",  # Феруловый пилинг
    "1a5f6c94-09af-4320-9664-1158c5b80cf3",  # Чистка + пилинг
)

#: Чем под pk сверяется предмет: ошибка в одном символе pk не должна
#: поставить юридический класс чужому канону.
NAME_MARKER = "пилинг"

#: Копия ``ServiceTemplate.LegalServiceClass.LEGAL_REVIEW_REQUIRED`` на момент
#: миграции; расхождение сторожится узлом.
LEGAL_REVIEW_REQUIRED = "legal_review_required"

#: Автор — владелец (``legal_class_confirmed_by``); кто исполнил — здесь.
SOURCE_REF = (
    "owner decision 07.10 (пакет service_family): пилинги на юр-проверку, DRF-2852; "
    "исполнил: окно каталога ayla-8d, миграцией"
)


class CannotAttribute(RuntimeError):
    """Канон на месте, а честного провенанса для записи нет."""


def close(template_model, user_model, *, confirmed_by_id, now) -> dict[str, int]:
    """Поставить класс четырём канонам; вернуть числа «закрыто» и «уже с классом»."""
    present = template_model.objects.filter(pk__in=PEELING_TEMPLATE_IDS)
    found = present.count()
    if not found:
        return {"found": 0, "closed": 0, "already_classed": 0}

    strangers = present.exclude(name__icontains=NAME_MARKER).count()
    if strangers:
        raise CannotAttribute(
            f"DRF-2852: под {strangers} из названных pk лежит не пилинг — класс не ставится"
        )
    if not confirmed_by_id:
        raise CannotAttribute("DRF-2852: именной учётки владельца нет — автора подтверждения нет")
    if not user_model.objects.filter(pk=confirmed_by_id, is_active=True).exists():
        raise CannotAttribute("DRF-2852: названного автора подтверждения в базе нет")

    closed = present.filter(legal_service_class__isnull=True).update(
        legal_service_class=LEGAL_REVIEW_REQUIRED,
        legal_class_confirmed_by_id=confirmed_by_id,
        legal_class_confirmed_at=now,
        legal_class_source_ref=SOURCE_REF,
    )
    return {"found": found, "closed": closed, "already_classed": found - closed}


def reopen(template_model) -> int:
    """Снять то, что поставил ``close``, — и только это; вернуть число строк.

    Отбор по pk, классу и основанию разом: канон, которому после накатки
    человек поставил другой класс или другое основание, остаётся как есть.
    """
    return template_model.objects.filter(
        pk__in=PEELING_TEMPLATE_IDS,
        legal_service_class=LEGAL_REVIEW_REQUIRED,
        legal_class_source_ref=SOURCE_REF,
    ).update(
        legal_service_class=None,
        legal_class_confirmed_by=None,
        legal_class_confirmed_at=None,
        legal_class_source_ref="",
    )
