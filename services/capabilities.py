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

from django.db.models import Q
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


def _client_facing_q(now: datetime, prefix: str = "") -> Q:
    """Подтверждено, поддержано, не истекло — в базе, а не только в памяти.

    ``prefix`` — путь до строки с основанием (``"capability__"`` у связи):
    связь проверяет возможность ПО БАЗЕ, а не по объекту в руках вызывающего,
    который мог устареть.
    """
    return (
        Q(**{f"{prefix}status": ClaimEvidence.Status.APPROVED})
        & Q(**{f"{prefix}claim_scope": ClaimEvidence.ClaimScope.SUPPORTED})
        & (Q(**{f"{prefix}valid_until__isnull": True}) | Q(**{f"{prefix}valid_until__gt": now}))
    )


def client_facing_capabilities(
    template: ServiceTemplate, *, now: datetime | None = None
) -> CapabilityReadout:
    """Возможности процедуры, которые можно сказать человеку, и явное состояние."""
    now = now or timezone.now()
    rows = list(
        # DRF-2743: запись словаря привязана к нескольким процедурам — та же
        # запись приходит для каждой из них с тем же ``claim_id``.
        ProcedureCapability.objects.filter(_client_facing_q(now), templates=template).order_by(
            "key"
        )
    )
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
    return tuple(
        CapabilityGoalLink.objects.filter(
            _client_facing_q(now),
            _client_facing_q(now, prefix="capability__"),
            capability_id=capability.pk,
            goal__is_active=True,
        )
        .select_related("goal")
        .order_by("goal__sort_order", "goal__key")
    )


def template_ids_helping_goal(goal_key: str, *, now: datetime | None = None) -> frozenset:
    """Шаблоны, у которых подтверждено «процедура умеет X» И «X помогает этой цели».

    DRF-2789 (R0 умного ранжирования): сильнейший сигнал глубины совпадения
    с целью. Правило то же, что у :func:`client_facing_goal_links`: оба
    утверждения подтверждены, поддержаны и не истекли, цель активна; вывод
    системы (``inference``) не считается. Здесь, а не у читателя: этот
    модуль — единственная санкционированная точка чтения знания.

    Возможность — запись словаря, привязанная к нескольким процедурам
    (DRF-2743): в ответ попадает каждая из них.
    """
    now = now or timezone.now()
    return frozenset(
        CapabilityGoalLink.objects.filter(
            _client_facing_q(now),
            _client_facing_q(now, prefix="capability__"),
            goal__key=goal_key,
            goal__is_active=True,
        ).values_list("capability__templates", flat=True)
    )


__all__ = [
    "CapabilityReadout",
    "KnowledgeState",
    "client_facing_capabilities",
    "client_facing_goal_links",
    "template_ids_helping_goal",
]
