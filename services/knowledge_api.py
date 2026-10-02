"""Внутренняя ручка чтения знания о процедуре (DRF-2724, контракт DRF-2719).

``GET /api/v1/internal/knowledge/procedures/`` — что о процедуре можно
сказать человеку. Обёртка над единственной санкционированной точкой чтения
(:mod:`services.capabilities`); своего фильтра здесь нет, чтобы у правила
«подтверждено, поддержано, не истекло» оставался один дом.

**Клиент её ещё не зовёт.** Ручка — первая половина теневого пути
(решение владельца 02.10, пакет A–F, блок D): она отдаёт знание, а ответ
человеку от этого не меняется.

### Вход

Ровно один предмет: ``template_id`` (каноническая процедура) или
``salon_service_id`` (услуга салона). Необязательно — ``goal_key``: вернуть
связи только с этой целью.

### Два независимых условия (решение владельца, блок D)

При входе от услуги салона знание отдаётся, только когда выполнены ОБА:
связь услуги с шаблоном подтверждена (``mapping_status = verified``) **и**
само утверждение подтверждено. Подтверждённая связь знания не даёт;
подтверждённое знание шаблона не говорится об услуге, чью связь никто не
подтвердил. При входе от шаблона связи нет — условие одно.

### Почему причина «неизвестно» на проводе одна

Решение владельца 11.09 (§1 п. 6): статус связи виден только во внутренней
очереди проверки — сторож
``services/tests/test_mapping_status_stays_internal.py``. Причина вида
«связь не подтверждена» в ответе выдала бы этот статус зеркалу бота другим
ключом. Поэтому наружу — одно ``no_confirmed_knowledge``, а точная причина
пишется в журнал.

### Что не покидает каталог

* ``prohibited_claim``, ``not_supported``, выводы системы, истёкшее — их не
  отдаёт уже слой чтения. Запрещённое уходит внутреннему проверяющему
  отдельным путём (блок C), не через эту ручку.
* ``text_professional`` — формулировка для мастера.
* ``confirmed_by`` — кто из людей подтвердил; наружу только вид
  подтверждения («человек» / «правило»).
* ``result_timeframe`` возможности — блок B: срок не говорится без оговорки
  о разбросе, а у возможности поля для оговорки нет. У связи с целью оно
  есть, поэтому ``result_horizon`` и ``course_pattern`` отдаются — и только
  вместе с ``variability_note``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from django.utils import timezone
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from users.permissions import IsInternalBearer
from users.response import error_response, success_response

from .capabilities import (
    KnowledgeState,
    client_facing_capabilities,
    client_facing_goal_links,
)
from .models import (
    CapabilityGoalLink,
    ClaimEvidence,
    GoalOption,
    ProcedureCapability,
    SalonService,
    ServiceTemplate,
)

logger = logging.getLogger(__name__)

#: Единственная причина ``unknown`` на проводе — см. докстринг модуля.
UNKNOWN_REASON = "no_confirmed_knowledge"


def _parse_uuid(raw: str | None) -> UUID | None:
    try:
        return UUID(str(raw))
    except (TypeError, ValueError):
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _provenance(row: ClaimEvidence) -> dict[str, Any]:
    """Основание утверждения — без личности подтвердившего."""
    return {
        "confirmed_kind": "human" if row.confirmed_by_id is not None else "rule",
        "confirmed_rule": row.confirmed_rule,
        "rule_version": row.rule_version,
        "confirmed_at": _iso(row.confirmed_at),
        "source_ref": row.source_ref,
        "evidence_source": row.evidence_source,
        "evidence_kind": row.evidence_kind,
        "valid_until": _iso(row.valid_until),
    }


def _goal_link(link: CapabilityGoalLink) -> dict[str, Any]:
    """Связь с целью. Курс и срок — только парой с оговоркой о разбросе.

    База не даёт сохранить ``course_pattern`` без ``variability_note``; для
    ``result_horizon`` такого ограничения в схеме нет, поэтому пара
    держится здесь: без оговорки срок не отдаётся.
    """
    qualified = bool(link.variability_note.strip())
    return {
        "claim_id": str(link.id),
        "kind": "goal_link",
        "goal_key": link.goal.key,
        "course_pattern": link.course_pattern if qualified else "",
        "result_horizon": link.result_horizon if qualified else "",
        "variability_note": link.variability_note,
        "limitations": link.limitations,
        "provenance": _provenance(link),
    }


def _capability(
    capability: ProcedureCapability, *, now: datetime, goal_key: str | None
) -> dict[str, Any]:
    links = client_facing_goal_links(capability, now=now)
    if goal_key is not None:
        links = tuple(link for link in links if link.goal.key == goal_key)
    return {
        "claim_id": str(capability.id),
        "kind": "capability",
        "key": capability.key,
        "text_client": capability.text_client,
        "expected_effect": capability.expected_effect,
        "limitations": capability.limitations,
        "provenance": _provenance(capability),
        "goal_links": [_goal_link(link) for link in links],
    }


def _payload(
    *,
    template: ServiceTemplate | None,
    salon_service: SalonService | None,
    claims: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    known = bool(claims)
    return {
        "state": KnowledgeState.KNOWN.value if known else KnowledgeState.UNKNOWN.value,
        "unknown_reason": None if known else UNKNOWN_REASON,
        "subject": {
            "template_id": str(template.id) if template is not None else None,
            "salon_service_id": str(salon_service.id) if salon_service is not None else None,
        },
        "as_of": now.isoformat(),
        "claims": claims,
    }


class InternalProcedureKnowledgeView(APIView):
    """GET /api/v1/internal/knowledge/procedures/ — см. докстринг модуля."""

    # Bearer бота — не JWT: пустой список аутентификаторов, как у остальных
    # внутренних ручек каталога.
    authentication_classes: list = []
    permission_classes = [IsInternalBearer]

    def get(self, request: Request) -> Response:
        template_raw = request.query_params.get("template_id")
        salon_service_raw = request.query_params.get("salon_service_id")
        if (template_raw is None) == (salon_service_raw is None):
            return error_response(
                "VALIDATION_ERROR",
                "exactly one of template_id / salon_service_id is required",
                status_code=400,
            )

        goal_key = request.query_params.get("goal_key")
        if goal_key is not None and not GoalOption.objects.filter(key=goal_key).exists():
            # Опечатка в ключе не должна читаться как «связей нет».
            return error_response("NOT_FOUND", "Goal not found", status_code=404)

        now = timezone.now()
        salon_service: SalonService | None = None
        template: ServiceTemplate | None

        if salon_service_raw is not None:
            salon_service_id = _parse_uuid(salon_service_raw)
            if salon_service_id is None:
                return error_response(
                    "VALIDATION_ERROR", "salon_service_id must be a valid UUID", status_code=400
                )
            salon_service = (
                SalonService.objects.select_related("template").filter(pk=salon_service_id).first()
            )
            if salon_service is None:
                return error_response("NOT_FOUND", "Salon service not found", status_code=404)
            template = salon_service.template
            if (
                template is None
                or salon_service.mapping_status != SalonService.MappingStatus.VERIFIED
            ):
                logger.info(
                    "knowledge.read unknown reason=%s salon_service=%s",
                    "no_template" if template is None else "mapping_not_verified",
                    salon_service.id,
                )
                return success_response(
                    _payload(template=template, salon_service=salon_service, claims=[], now=now)
                )
        else:
            template_id = _parse_uuid(template_raw)
            if template_id is None:
                return error_response(
                    "VALIDATION_ERROR", "template_id must be a valid UUID", status_code=400
                )
            template = ServiceTemplate.objects.filter(pk=template_id).first()
            if template is None:
                return error_response("NOT_FOUND", "Template not found", status_code=404)

        readout = client_facing_capabilities(template, now=now)
        claims = [
            _capability(capability, now=now, goal_key=goal_key)
            for capability in readout.capabilities
        ]
        if not claims:
            logger.info("knowledge.read unknown reason=no_approved_claims template=%s", template.id)
        return success_response(
            _payload(template=template, salon_service=salon_service, claims=claims, now=now)
        )
