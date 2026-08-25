"""Гейты согласия домена wellness — fail-closed (GOALS-R5/R6, amendments C/E).

Контракт: docs/PROPOSAL_GOALS_MODEL_FINAL.md §7.

Две именованные точки разной природы:
- Gate D (`goal_intention_gate`) — persistent `explicit_goal`; fail-closed
  до утверждения отдельного scope `goal_memory` (`preference_memory` не
  расширяется, GOALS-R6).
- Gate O (`body_observation_gate`) — body observations; fail-closed до
  Registry amendment + Privacy/Legal + verified consent integration
  (GOALS-R5/R6). Никаких bypass / feature-flag разрешений.

AMENDMENT E: сервис НЕ принимает `consent=True/False`. Единственный вход —
типизированный `ConsentAttestation` (кто утверждал, на каком основании,
scope, document_version). Топология подключения не выбрана до Registry
amendment; fail-closed сохраняется даже с валидной attestation.

AMENDMENT C: гейт защищает product processing, не права субъекта. Это
свойство гейта, не исключение в вызывающем коде: гейт принимает `purpose`.
Для `purpose=subject_rights` (inspect/export/correction/deletion уже
сохранённого, в том числе после revoke) гейт НЕ блокирует.

Fail-closed доказуем, не заявлен (§7.3): постоянная CI-проверка —
`wellness/tests/test_fail_closed.py`. Тест краснеет при любом ослаблении.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from users.models import User


class Purpose:
    """Назначение доступа (amendment C). Неизвестное значение — отказ."""

    PROCESSING = "processing"
    SUBJECT_RIGHTS = "subject_rights"


@dataclass(frozen=True)
class ConsentAttestation:
    """Типизированное основание согласия (amendment E).

    Заменяет boolean `consent`: фиксирует scope, кто утверждал
    (`authority`), на каком основании (`provenance`) и версию документа.
    В аудит пишется заявленное основание — видно, на каком основании
    запись прошла. Пока ни один гейт не принимает никакую attestation
    для `purpose=processing` — trusted source не подключён.
    """

    scope: str
    authority: str
    provenance: str
    document_version: str
    captured_at: datetime


@dataclass(frozen=True)
class GateDecision:
    """Исход гейта. `reason_code` — машиночитаемый код отказа."""

    allowed: bool
    reason_code: str


def goal_intention_gate(
    attestation: ConsentAttestation | None,
    purpose: str,
) -> GateDecision:
    """Gate D — намерение (persistent explicit_goal), GOALS-R6.

    Fail-closed до утверждения scope `goal_memory`: для
    `purpose=processing` — всегда отказ, даже с валидной attestation
    (scope не утверждён, trusted source не подключён). Права субъекта
    не блокируются (amendment C).
    """
    if purpose == Purpose.SUBJECT_RIGHTS:
        return GateDecision(allowed=True, reason_code="subject_rights")
    if purpose == Purpose.PROCESSING:
        return GateDecision(allowed=False, reason_code="scope_not_approved")
    return GateDecision(allowed=False, reason_code="unknown_purpose")


def body_observation_gate(
    attestation: ConsentAttestation | None,
    purpose: str,
) -> GateDecision:
    """Gate O — body observations, GOALS-R5/R6.

    Fail-closed до Registry amendment + Privacy/Legal + verified consent
    integration: для `purpose=processing` — всегда отказ, даже с валидной
    attestation (trusted source не подключён, topology не выбрана).
    Права субъекта не блокируются — даже после revoke (amendment C).
    """
    if purpose == Purpose.SUBJECT_RIGHTS:
        return GateDecision(allowed=True, reason_code="subject_rights")
    if purpose == Purpose.PROCESSING:
        return GateDecision(allowed=False, reason_code="blocked_pending_privacy_legal")
    return GateDecision(allowed=False, reason_code="unknown_purpose")


def record_outcome(
    user: "User",
    *,
    target: str,
    statement_text: str,
    attestation: ConsentAttestation | None = None,
) -> GateDecision:
    """Публичный writer DesiredOutcome — идёт через Gate D (§7.1, §9.1).

    Сейчас всегда возвращает отказ и НЕ пишет: scope `goal_memory` не
    утверждён (GOALS-R6, GO). Никаких флагов/обходов. Включение —
    отдельное решение после readback схемы и review.
    """
    return goal_intention_gate(attestation, purpose=Purpose.PROCESSING)


def record_observation(
    user: "User",
    *,
    observation_type: str,
    value_numeric: Decimal | None = None,
    value_ordinal: int | None = None,
    instrument: str | None = None,
    attestation: ConsentAttestation | None = None,
) -> GateDecision:
    """Публичный writer ProgressObservation — идёт через Gate O (§7.2).

    Сейчас всегда возвращает отказ и НЕ пишет: нет Registry amendment +
    Privacy/Legal + verified consent integration (GOALS-R5, GO).
    Никаких bypass / feature-flag разрешений.
    """
    return body_observation_gate(attestation, purpose=Purpose.PROCESSING)
