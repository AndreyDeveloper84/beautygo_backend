"""Смена согласия, доставленная ботом, — применение на стороне каталога (DRF-2776).

Реестр согласий живёт в боте (``apps/consent/models.py::ConsentRecord``);
каталог его не дублирует. Решение владельца 05.10 (D, DRF-2434): смену
согласия получают системы, чьё поведение от неё зависит. В каталоге это:

* ``personal_calculation`` отозвано → стираются параметры тела и всё, что из
  них посчитано (:func:`erase_personal_calculation_inputs`, тот же путь, что у
  ручки «Отключить и удалить», исполнителя удаления и «забыть всё»);
* ``health`` отозвано → стираются флаги здоровья и ориентиры от них
  (:func:`erase_health_flags`);
* ``food_diary_processing`` в обе стороны → только состояние: по нему
  еженедельный бьюти-инсайт не шлётся тому, кто отозвал (D-2).

Остальные типы каталогу не нужны — ``ignored``, но квитанция пишется:
журнал «изменение и результат доставки» — тоже из решения владельца.

### Порядок — по ``granted_at``, а не по приходу

Бот доставляет по крайней мере один раз и не гарантирует порядок. Поэтому на
(человек, тип) хранится последнее известное состояние с его ``granted_at``:

* событие старше известного → ``stale``, ничего не делается;
* при равном ``granted_at`` побеждает отзыв (fail-closed);
* стирание выполняется, только если отзыв — самое новое известное событие
  по типу. Иначе запоздавший отзыв стёр бы данные, отданные уже ПОСЛЕ
  повторного согласия;
* повторное согласие стёртое не возвращает — удалено значит удалено.

### Идемпотентность — по ``event_id``

Квитанция уникальна по ``event_id``; повтор отвечает ``duplicate`` с тем, что
стёрла первая доставка.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from django.db import IntegrityError, transaction

from nutrition.management.commands.clear_targets_without_provenance import (
    PROVENANCE_FIELDS,
    TARGET_FIELDS,
)
from nutrition.services.health_withdrawal import erase_health_flags
from nutrition.services.personal_calculation_withdrawal import (
    DERIVED_EMPTY_BY_FIELD,
    WITHDRAWN_FIELDS,
    erase_personal_calculation_inputs,
)
from nutrition.services.targets_state import KIND_SOURCE_FIELD, KIND_STAMP_FIELD
from users.models import ConsentEventReceipt, ConsentState, User

logger = logging.getLogger(__name__)

PERSONAL_CALCULATION = "personal_calculation"
HEALTH = "health"
FOOD_DIARY_PROCESSING = "food_diary_processing"

#: Типы, от которых зависит поведение каталога. Остальные — ``ignored``.
HANDLED_TYPES = frozenset({PERSONAL_CALCULATION, HEALTH, FOOD_DIARY_PROCESSING})

Outcome = ConsentEventReceipt.Outcome


@dataclass(frozen=True)
class ConsentEvent:
    event_id: str
    consent_type: str
    granted: bool
    granted_at: datetime
    granted_via: str = ""


@dataclass(frozen=True)
class AppliedEvent:
    event_id: str
    outcome: str
    erased: tuple[str, ...]

    def as_payload(self) -> dict:
        return {"event_id": self.event_id, "outcome": self.outcome, "erased": list(self.erased)}


#: Всё, что стирает отзыв ``personal_calculation``, — для квитанции. Сама
#: функция отвечает своим вызывающим только входами (её ответ — контракт
#: пользовательской ручки «Отключить и удалить»), а квитанция обязана назвать
#: и ориентиры, и провенанс, и выведенное: «журнал фиксирует результат».
PERSONAL_CALCULATION_ERASED: tuple[str, ...] = (
    *WITHDRAWN_FIELDS,
    *DERIVED_EMPTY_BY_FIELD,
    *TARGET_FIELDS,
    *PROVENANCE_FIELDS,
    *KIND_SOURCE_FIELD.values(),
    *KIND_STAMP_FIELD.values(),
)


def _erase(user, consent_type: str) -> tuple[str, ...]:
    if consent_type == PERSONAL_CALCULATION:
        outcome = erase_personal_calculation_inputs(user)
        return PERSONAL_CALCULATION_ERASED if outcome.profile_existed else ()
    if consent_type == HEALTH:
        return tuple(erase_health_flags(user).erased)
    return ()


def _is_stale(state: ConsentState | None, event: ConsentEvent) -> bool:
    """Старше известного — или в ту же секунду, но согласие против отзыва."""
    if state is None:
        return False
    if event.granted_at < state.granted_at:
        return True
    return event.granted_at == state.granted_at and event.granted and not state.granted


def apply_consent_event(user, event: ConsentEvent) -> AppliedEvent:
    """Применить одну смену согласия. ``user is None`` — человек каталогу не известен."""
    try:
        with transaction.atomic():
            seen = ConsentEventReceipt.objects.filter(event_id=event.event_id).first()
            if seen is not None:
                return AppliedEvent(event.event_id, Outcome.DUPLICATE, tuple(seen.erased))

            erased: tuple[str, ...] = ()
            if user is not None:
                # Все смены согласия ОДНОГО человека — по очереди. Блокировка
                # строки состояния не годится: при первом событии строки ещё
                # нет, и два события разошлись бы — старшее записало бы своё
                # поверх младшего без проверки порядка, а запоздавший отзыв
                # стёр бы данные после повторного согласия. Строка человека
                # есть всегда.
                User.objects.select_for_update().filter(pk=user.pk).first()
            if user is None:
                outcome = Outcome.NO_SUBJECT
            elif event.consent_type not in HANDLED_TYPES:
                outcome = Outcome.IGNORED
            else:
                state = (
                    ConsentState.objects.select_for_update()
                    .filter(user=user, consent_type=event.consent_type)
                    .first()
                )
                if _is_stale(state, event):
                    outcome = Outcome.STALE
                else:
                    ConsentState.objects.update_or_create(
                        user=user, consent_type=event.consent_type,
                        defaults={
                            "granted": event.granted,
                            "granted_at": event.granted_at,
                            "event_id": event.event_id,
                        },
                    )
                    if not event.granted:
                        erased = _erase(user, event.consent_type)
                    outcome = Outcome.APPLIED

            ConsentEventReceipt.objects.create(
                event_id=event.event_id,
                user=user,
                consent_type=event.consent_type,
                granted=event.granted,
                granted_at=event.granted_at,
                granted_via=event.granted_via,
                outcome=outcome,
                erased=list(erased),
            )
    except IntegrityError:
        # Та же доставка пришла параллельно и записала квитанцию первой.
        seen = ConsentEventReceipt.objects.get(event_id=event.event_id)
        return AppliedEvent(event.event_id, Outcome.DUPLICATE, tuple(seen.erased))

    logger.info(
        "consent.event_applied type=%s granted=%s outcome=%s erased=%d",
        event.consent_type, event.granted, outcome, len(erased),
    )
    return AppliedEvent(event.event_id, outcome, erased)


def food_diary_withdrawn_user_ids():
    """Кому бьюти-инсайт не шлётся: последнее известное по ``food_diary_processing`` — отзыв.

    Решение (б) главного окна 05.10: неизвестное состояние — не отзыв. Отзыв,
    как только событие дошло, останавливает инсайт.
    """
    return ConsentState.objects.filter(
        consent_type=FOOD_DIARY_PROCESSING, granted=False,
    ).values_list("user_id", flat=True)


__all__ = [
    "AppliedEvent",
    "ConsentEvent",
    "HANDLED_TYPES",
    "apply_consent_event",
    "food_diary_withdrawn_user_ids",
]
