"""Заявки на удаление аккаунта — приём и чтение (§7 свода, DRF-1699, срез D1).

Здесь только жизнь заявки как записи: завести, найти открытую, отдать по
``request_id``. Само стирание — срез D3 (исполнитель); он читает заявку
отсюда и меняет её статус. Ни одна функция этого модуля ничего не стирает.

### Почему «завести» идемпотентно

Человек может нажать дважды, Mini App — повторить запрос после таймаута.
Вторая открытая заявка ничего не добавила бы (отменить удаление после
начала нельзя, §7), а ``request_id`` на экране стал бы другим — и человек
видел бы два номера одного удаления. Открытая заявка возвращается как
есть; гонка двух одновременных «завести» разрешается частичным уникальным
индексом ``deletionrequest_one_open_per_user``: проигравший перечитывает
победителя.

### Что считается «открытой»

``REQUESTED``, ``PROCESSING`` и ``FAILED``. Последнее — намеренно: сбой
исполнителя не закрывает заявку и не заводит новую; повтор исполнения
идёт по той же записи, и человек продолжает видеть тот же ``request_id``.
Закрывает заявку только ``COMPLETED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from users.models import DeletionRequest, User


@dataclass(frozen=True)
class EnsuredDeletionRequest:
    request: DeletionRequest
    created: bool


def open_request_for(user: User) -> DeletionRequest | None:
    """Открытая заявка человека или ``None``."""
    return (
        DeletionRequest.objects
        .filter(user=user, status__in=DeletionRequest.OPEN_STATUSES)
        .first()
    )


def ensure_deletion_request(user: User, *, initiator: str) -> EnsuredDeletionRequest:
    """Открытая заявка человека — существующая или только что заведённая.

    ``deadline_at`` считается здесь, один раз, от момента приёма:
    §7 требует показать человеку **точную** крайнюю дату, и она не должна
    зависеть от того, когда экран перечитает заявку.
    """
    existing = open_request_for(user)
    if existing is not None:
        return EnsuredDeletionRequest(request=existing, created=False)

    now = timezone.now()
    try:
        with transaction.atomic():
            created = DeletionRequest.objects.create(
                user=user,
                initiator=initiator,
                requested_at=now,
                deadline_at=now + timedelta(days=DeletionRequest.DEADLINE_DAYS),
            )
    except IntegrityError:
        # Проиграли гонку: победитель уже открыл заявку — она и есть ответ.
        existing = open_request_for(user)
        if existing is None:  # pragma: no cover - индекс гарантирует строку
            raise
        return EnsuredDeletionRequest(request=existing, created=False)
    return EnsuredDeletionRequest(request=created, created=True)


def current_request_for(user: User) -> DeletionRequest | None:
    """Заявка, которую экран показывает как «текущую».

    Открытая, если есть; иначе последняя завершённая — человек, чьё
    удаление уже исполнено, вправе видеть номер и дату завершения, а не
    «заявок нет». ``None`` — заявок не было вовсе.
    """
    open_one = open_request_for(user)
    if open_one is not None:
        return open_one
    return DeletionRequest.objects.filter(user=user).order_by("-requested_at").first()


def get_deletion_request(user: User, request_id: UUID) -> DeletionRequest | None:
    """Заявка по номеру — только своя: чужой номер неотличим от несуществующего."""
    return DeletionRequest.objects.filter(pk=request_id, user=user).first()


def as_payload(req: DeletionRequest) -> dict:
    """Форма ответа наружу — одна на POST и GET, чтобы экран читал одно и то же."""
    return {
        "request_id": str(req.pk),
        "status": req.status,
        "requested_at": req.requested_at.isoformat(),
        "deadline_at": req.deadline_at.isoformat(),
        "completed_at": req.completed_at.isoformat() if req.completed_at else None,
        "is_open": req.is_open,
    }
