"""Правила ввода знания о процедурах — одни на все входы (DRF-2717).

Знание о процедурах (:class:`~services.models.ProcedureCapability`,
:class:`~services.models.CapabilityGoalLink`, DRF-2606) вносится двумя входами:
формой в Django-admin и засевом из файла (``seed_procedure_knowledge``). База
отклоняет неподтверждённое основание сама — ограничениями ``CHECK``. Но отказ
базы приходит именем ограничения и после того, как человек нажал «Сохранить»;
а два входа с двумя своими проверками однажды разойдутся.

Поэтому условия ограничений базы записаны здесь один раз, словами для человека
и по полям, и оба входа спрашивают их ДО записи. База остаётся последним
рубежом, а не единственным.

Здесь только форма утверждения. Содержание — что процедура умеет и чем это
подтверждено — собирает владелец; кодом знание не выдумывается.
"""

from __future__ import annotations

import re

#: То же выражение, что в ``capabilitygoallink_course_not_a_bare_number``.
_BARE_NUMBER = re.compile(r"^\s*[0-9]+([.,][0-9]+)?\s*$")

APPROVED = "approved"


def provenance_errors(*, status: str, source_ref: str, has_confirmer: bool) -> dict[str, str]:
    """Чего не хватает, чтобы строку можно было назвать подтверждённой.

    Зеркало ``<класс>_approved_requires_provenance``: подтверждение — это
    решение, и без того, кто (или какое правило) его принял и на что сослался,
    оно через месяц неотличимо от умолчания. Момент подтверждения ставит вход
    сам (форма — при сохранении, засев — из файла), здесь его нет.

    Возвращает ``{поле: сообщение}``; пусто — можно сохранять.
    """
    if status != APPROVED:
        return {}
    errors: dict[str, str] = {}
    if not (source_ref or "").strip():
        errors["source_ref"] = (
            "Подтверждённое утверждение должно ссылаться на источник: "
            "укажите ссылку или название документа."
        )
    if not has_confirmer:
        errors["status"] = (
            "Подтвердить может только человек или названное правило владельца — "
            "не указано ни то, ни другое."
        )
    return errors


def course_errors(
    *,
    course_pattern: str,
    variability_note: str,
    evidence_source: str,
    source_ref: str,
) -> dict[str, str]:
    """Что не так с описанием курса у связи возможности с целью.

    Зеркало двух ограничений базы: курс не хранится голым числом и не
    хранится без оговорки о разбросе и основания (решение владельца 29.09 —
    «обычно курс N» без оговорки и источника есть маркетинговое обещание).
    """
    pattern = (course_pattern or "").strip()
    if not pattern:
        return {}
    errors: dict[str, str] = {}
    if _BARE_NUMBER.match(pattern):
        errors["course_pattern"] = (
            "Курс описывается словами, а не одним числом: «10» нельзя, "
            "«обычно рассматривается как курс сеансов» — можно."
        )
    if not (variability_note or "").strip():
        errors["variability_note"] = (
            "У курса должна быть оговорка о разбросе: от чего зависит, у кого бывает иначе."
        )
    if not (evidence_source or "").strip():
        errors["evidence_source"] = "У курса должен быть источник: откуда это известно."
    if not (source_ref or "").strip():
        errors["source_ref"] = "У курса должна быть ссылка на источник."
    return errors


__all__ = ["APPROVED", "course_errors", "provenance_errors"]
