"""Мастер подтверждает клиентскую запись — DRF-2785 (вариант «в» владельца 05.10).

«✅ Подтверждаю» в боте мастера. Это ответ мастера, а не переход статуса:
на пилоте запись без предоплаты становится CONFIRMED в момент создания
(DRF-1007), и владелец выбрал не менять модель записи — без нового статуса
и без ожидания. Поэтому здесь нет ни ``status``, ни слота, ни политики
отмены: пишется только факт ответа и событие ``booking.acknowledged``,
по которому бот скажет клиенту «мастер подтвердил запись».

Подтверждение относится к конкретному времени визита — оно хранится вместе
с ``version`` записи. После переноса версия растёт, и прежнее подтверждение
видно как устаревшее, а повторное «Подтверждаю» снова пишет факт и снова
шлёт событие: мастер подтвердил уже НОВОЕ время.

**Блокировка — дело вызывающего**, как в ``completion``: функция ждёт строку
под ``select_for_update`` внутри открытой транзакции. Иначе два нажатия
подряд оба увидели бы «ещё не подтверждено» и отправили два события.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.utils import timezone

from appointments.domain.value_objects import (
    ACTIVE_BOOKING_STATUSES,
    BookingStatus,
    OperationalActor,
    envelope_actor_for,
)


@dataclass(frozen=True)
class AcknowledgementOutcome:
    #: ``True`` — факт записан сейчас и событие ушло; ``False`` — эта же
    #: версия записи уже была подтверждена, ничего не изменилось.
    recorded: bool


def acknowledge_booking(appointment, *, acknowledged_by_user_id) -> AcknowledgementOutcome:
    """Записать «мастер подтвердил» на заблокированную строку и выпустить событие.

    Подтверждать можно только живую запись (держит слот: ожидает оплаты или
    подтверждена). Отменённая, завершённая или неявка — ``ValidationError``:
    подтверждать там нечего, и вызывающий отвечает 422, а не молча 200.
    """
    if BookingStatus(appointment.status) not in ACTIVE_BOOKING_STATUSES:
        raise ValidationError(
            f"Cannot acknowledge appointment with status '{appointment.status}'."
        )

    if appointment.master_acknowledged_version == appointment.version:
        return AcknowledgementOutcome(recorded=False)

    appointment.master_acknowledged_at = timezone.now()
    appointment.master_acknowledged_version = appointment.version
    appointment.save(update_fields=[
        "master_acknowledged_at", "master_acknowledged_version", "updated_at",
    ])
    emit_booking_acknowledged(appointment, acknowledged_by_user_id=acknowledged_by_user_id)
    return AcknowledgementOutcome(recorded=True)


def emit_booking_acknowledged(appointment, *, acknowledged_by_user_id):
    """Строка outbox ``booking.acknowledged``. Только идентификаторы и время — без людей.

    ``user_id`` конверта — ЗАТРОНУТЫЙ человек, то есть клиент, как у
    ``booking.completed`` (event-contract §2.2): бот адресует текст ему.
    Кто нажал — ``acknowledged_by`` в data; актор конверта — грубый
    трёхзначный словарь и мастера не называет.
    """
    from appointments.infrastructure.outbox import (
        emit_outbox_event, safe_tenant_id,
    )
    from appointments.models import OutboxEvent

    return emit_outbox_event(
        topic=OutboxEvent.Topic.BOOKING_ACKNOWLEDGED,
        data={
            "appointment_id": str(appointment.id),
            "client_id": str(appointment.client_id),
            "specialist_id": str(appointment.specialist_id),
            "start_at": appointment.start_datetime.isoformat(),
            "version": appointment.version,
            "acknowledged_at": appointment.master_acknowledged_at.isoformat(),
            "acknowledged_by": OperationalActor.SPECIALIST.value,
            "acknowledged_by_user_id": str(acknowledged_by_user_id),
        },
        user_id=appointment.client_id,
        tenant_id=safe_tenant_id(appointment, context="booking.acknowledged"),
        actor=envelope_actor_for(OperationalActor.SPECIALIST.value),
    )
