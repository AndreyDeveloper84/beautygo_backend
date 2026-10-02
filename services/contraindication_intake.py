"""Правила ввода структурированных противопоказаний — одни на форму и на файл (DRF-2741).

Решение владельца №2 от 02.10: слот C8 («кому нельзя, когда нужен врач»)
хранится структурированным набором — условие, действие из закрытого списка,
источник, область применения, дата пересмотра, рецензент. «Один
непроверяемый текстовый блок недостаточен».

Носитель — :class:`~services.models.ProcedureContraindication`: строка на
условие. Здесь — то, что обе двери ввода (форма админки и засев из файла)
спрашивают до записи; база повторяет главное ограничениями.

Что обязано быть у подтверждённой строки
----------------------------------------
Источник (не заглушка), вид доказательства из закрытого списка (DRF-2742),
дата пересмотра, отметка рецензента, область применения (хотя бы одна
процедура или категория) и подпись подтвердившего. Условие и действие
обязательны у любой строки: без них строки нет.

Импорт — не проверка и не подтверждение
---------------------------------------
Из файла строка приходит только черновиком. Ни статус, ни отметка рецензента,
ни подпись в файле не принимаются — такие ключи в нём неизвестны.

Чего эти проверки НЕ могут: убедиться, что условие сформулировано верно, а
действие ему соответствует. Это работа рецензента — владелец назвал слот C8
требующим профильной проверки всегда.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from services.knowledge_intake import APPROVED, evidence_kind_errors, source_ref_problem

#: Действия — закрытый список (решение владельца №2). Копия
#: ``ProcedureContraindication.Action``; равенство держит узел.
ACTIONS = ("exclude", "postpone", "refer_to_doctor", "emergency")

#: Ключи строки файла. Статуса, отметки рецензента и подписи среди них нет
#: намеренно: из файла они не приезжают.
FILE_KEYS = frozenset({
    "key", "condition", "action", "action_note", "template_codes", "scope_note",
    "source_ref", "evidence_source", "evidence_kind", "review_date",
})


def approval_errors(
    *,
    status: str,
    source_ref: str,
    evidence_kind: str,
    review_date: date | None,
    has_scope: bool,
) -> dict[str, str]:
    """Чего не хватает, чтобы противопоказание можно было назвать подтверждённым.

    Отметку рецензента и подпись вход проверяет сам — это действия, а не поля.
    Вид доказательства вне списка — ошибка у любой строки, не только у
    подтверждённой.
    """
    errors = dict(evidence_kind_errors(status=status, evidence_kind=evidence_kind))
    if status != APPROVED:
        return errors
    problem = source_ref_problem(source_ref)
    if problem:
        errors["source_ref"] = "Подтверждённое противопоказание должно ссылаться на источник. " + problem
    if review_date is None:
        errors["review_date"] = (
            "У подтверждённого противопоказания должна быть дата пересмотра: "
            "когда его нужно проверить заново."
        )
    if not has_scope:
        errors["templates"] = (
            "Укажите, к чему противопоказание применяется: хотя бы одну процедуру или категорию."
        )
    return errors


def parse_review_date(raw: Any) -> tuple[date | None, str | None]:
    """Дата пересмотра из файла: ``(дата, None)`` либо ``(None, чем плоха)``."""
    if raw in (None, ""):
        return None, None
    if not isinstance(raw, str):
        return None, f"review_date — ожидалась строка ГГГГ-ММ-ДД, получено {type(raw).__name__}"
    try:
        return date.fromisoformat(raw.strip()), None
    except ValueError:
        return None, f"review_date — нужна существующая дата ГГГГ-ММ-ДД, получено «{raw}»"


__all__ = ["ACTIONS", "FILE_KEYS", "approval_errors", "parse_review_date"]
