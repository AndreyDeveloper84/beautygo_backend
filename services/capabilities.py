"""Что можно сказать человеку о процедуре — единственная санкционированная точка чтения (DRF-2606).

Решение владельца 29.09: возможность процедуры (:class:`ProcedureCapability`)
и её связь с целью (:class:`CapabilityGoalLink`) — две таблицы, у каждой своё
основание. Этот модуль — не путь ответа (его в листе не строили), а место,
где держится правило, которое любой будущий читатель обязан унаследовать:

* **вывод системы ≠ подтверждённое человеком.** ``inference`` не отдаётся;
  та же строка после подтверждения — отдаётся;
* **«неизвестно» — не разрешение.** У процедуры без подтверждённых
  возможностей состояние ``UNKNOWN``, а не пустой список, читаемый как
  «ничего не умеет» или «можно говорить что угодно» — тот же принцип, что у
  health-gate (``_booking_guards``: ``None`` → отказ, а не пропуск);
* **``NOT_SUPPORTED`` и ``PROHIBITED_CLAIM`` не отдаются никогда.** Пригодность
  для клиента не хранится флагом — она вычисляется (подтверждено И
  ``SUPPORTED`` И не истекло), поэтому запрещённое утверждение не может
  «стать клиентским» ни правкой одного поля, ни забывчивостью. Реальный
  запрет в живом ответе — дело будущего читателя; здесь — носитель.

Чего здесь нет и почему. Поля «курс / число процедур» в перечне владельца
нет, поэтому фраза «тебе нужно 10 процедур» невозможна по построению: её
негде взять. Заводить такое поле — это содержание, которое собирает владелец.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from django.utils import timezone

from services.models import CapabilityGoalLink, ClaimEvidence, ProcedureCapability, ServiceTemplate


class KnowledgeState(str, Enum):
    #: Есть хотя бы одна подтверждённая, поддержанная, не истёкшая возможность.
    KNOWN = "known"
    #: Подтверждённого нет — ни одной строки или только выводы/запреты/истёкшее.
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CapabilityReadout:
    state: KnowledgeState
    capabilities: tuple[ProcedureCapability, ...] = field(default_factory=tuple)


def _client_facing_filter(now: datetime) -> dict:
    return {
        "status": ClaimEvidence.Status.APPROVED,
        "claim_scope": ClaimEvidence.ClaimScope.SUPPORTED,
    }


def client_facing_capabilities(
    template: ServiceTemplate, *, now: datetime | None = None
) -> CapabilityReadout:
    """Возможности процедуры, которые можно сказать человеку, и явное состояние."""
    now = now or timezone.now()
    rows = [
        c
        for c in ProcedureCapability.objects.filter(
            template=template, **_client_facing_filter(now)
        ).order_by("key")
        if c.is_client_facing(now=now)
    ]
    if not rows:
        return CapabilityReadout(state=KnowledgeState.UNKNOWN)
    return CapabilityReadout(state=KnowledgeState.KNOWN, capabilities=tuple(rows))


def client_facing_goal_links(
    capability: ProcedureCapability, *, now: datetime | None = None
) -> tuple[CapabilityGoalLink, ...]:
    """Цели, которым возможность помогает, — только если подтверждены ОБА утверждения.

    «Процедура умеет X» и «X помогает цели Y» — два решения: связь не
    говорится, пока не подтверждена сама возможность, и наоборот.
    """
    now = now or timezone.now()
    if not capability.is_client_facing(now=now):
        return ()
    return tuple(
        link
        for link in CapabilityGoalLink.objects.filter(
            capability=capability, **_client_facing_filter(now)
        ).select_related("goal")
        if link.is_client_facing(now=now) and link.goal.is_active
    )


__all__ = [
    "CapabilityReadout",
    "KnowledgeState",
    "client_facing_capabilities",
    "client_facing_goal_links",
]
