"""«Своя услуга» мастера = заявка о разрыве канона к владельцу (G6 / D6, M9, DRF-1801).

Решение владельца (12.09, D6 — (а)) и принцип DRF-1349 08:31 «канон
первичен»: услуга, которой в каноне нет, — **не новая каноническая строка
и не предложение мастера**, а заявка к владельцу:

    PENDING              ничего не создаёт в каноне: ни ServiceTemplate,
                         ни SalonService, ни SpecialistService — клиенту
                         не видна и в подбор не попадает по построению
    APPROVED             владелец нашёл существующий или осознанно создал
                         канонический шаблон и связал заявку с ним
    NEEDS_CLARIFICATION  мастеру уходит текст вопроса
    REJECTED             мастеру уходит понятная причина

Решает **только человек в Django-admin** (§143: актор обязателен) — ни
LLM, ни бот: внутренняя ручка умеет завести заявку и прочитать её, но не
решить. SLA в P0 нет.

### Что APPROVED НЕ делает

Не создаёт предложение мастера. «Offer мастера создаётся на канонической
строке через обычный controlled path» — это M8, которого на dev нет;
до него связанная заявка только называет шаблон, и это видно в ответе
(``resolved_template``), а не спрятано автосозданием.

### Подсказка «похожая услуга»

:func:`similar_templates` ищет канон по подтверждённым синонимам и по
точному (нормализованному) названию — и **ничего не связывает**: синоним
«находит канон, и на этом его полномочия кончаются» (§93). Выбор
«Выбрать эту услугу» / «Добавить мою» делает мастер на экране.
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

from services.models import CanonGapRequest, ServiceTemplate, ServiceTemplateSynonym
from services.normalization import normalize_service_name

S = CanonGapRequest.Status

#: Четыре слова мастеру (фриз §10.1) — одно на статус, других нет.
STATUS_LABELS: dict[str, str] = {
    S.PENDING: "На проверке",
    S.APPROVED: "Подтверждена",
    S.NEEDS_CLARIFICATION: "Нужно уточнение",
    S.REJECTED: "Отклонена",
}

#: Из каких состояний владелец может принять решение. APPROVED и REJECTED
#: терминальны: пересмотр — новая заявка, а не перезапись чужого решения.
DECIDABLE_FROM = frozenset({S.PENDING, S.NEEDS_CLARIFICATION})


class CanonGapDecisionError(ValueError):
    """Решение не может быть записано — и вот почему (имя в ``code``)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class SimilarTemplate:
    template_id: str
    name: str
    matched_by: str  # "synonym" | "canonical_name"


def similar_templates(name: str, *, limit: int = 5) -> list[SimilarTemplate]:
    """Канон, похожий на название мастера. Только чтение."""
    key = normalize_service_name(name)
    if not key:
        return []
    found: dict[str, SimilarTemplate] = {}
    for syn in (
        ServiceTemplateSynonym.objects.filter(normalized=key)
        .select_related("template")
        .order_by("template__name")
    ):
        tid = str(syn.template_id)
        found.setdefault(tid, SimilarTemplate(tid, syn.template.name, "synonym"))
    for tpl in ServiceTemplate.objects.filter(name__iexact=name.strip()).order_by("name"):
        if normalize_service_name(tpl.name) == key:
            found.setdefault(str(tpl.pk), SimilarTemplate(str(tpl.pk), tpl.name, "canonical_name"))
    return list(found.values())[:limit]


def create_request(specialist, *, name: str, description: str, duration_minutes: int, price) -> CanonGapRequest:
    """Завести заявку ``PENDING``. Больше ничего не пишется."""
    return CanonGapRequest.objects.create(
        tenant=specialist.tenant,
        specialist=specialist,
        name=name.strip(),
        description=(description or "").strip(),
        duration_minutes=duration_minutes,
        price=price,
    )


def decide(
    req: CanonGapRequest,
    *,
    actor,
    status: str,
    template: ServiceTemplate | None = None,
    question: str = "",
    reason: str = "",
) -> CanonGapRequest:
    """Решение владельца. Единственный писатель статуса.

    Актор обязателен и должен быть живым сотрудником — решение без автора
    через месяц читается как умолчание (§143).
    """
    if actor is None or not getattr(actor, "is_authenticated", False) or not getattr(actor, "is_staff", False):
        raise CanonGapDecisionError("actor_required", "Решение по заявке принимает сотрудник в админке.")
    if status not in (S.APPROVED, S.NEEDS_CLARIFICATION, S.REJECTED):
        raise CanonGapDecisionError("not_a_decision", f"«{status}» — не решение.")

    with transaction.atomic():
        locked = CanonGapRequest.objects.select_for_update().get(pk=req.pk)
        if locked.status not in DECIDABLE_FROM:
            raise CanonGapDecisionError(
                "already_decided",
                f"Заявка уже {STATUS_LABELS[locked.status].lower()}: решение не перезаписывается.",
            )
        if status == S.APPROVED and template is None:
            raise CanonGapDecisionError("template_required", "Подтверждение связывает заявку с каноническим шаблоном.")
        if status == S.NEEDS_CLARIFICATION and not question.strip():
            raise CanonGapDecisionError("question_required", "Напишите вопрос мастеру.")
        if status == S.REJECTED and not reason.strip():
            raise CanonGapDecisionError("reason_required", "Напишите мастеру понятную причину.")

        locked.status = status
        locked.resolved_template = template if status == S.APPROVED else None
        locked.clarification_question = question.strip() if status == S.NEEDS_CLARIFICATION else ""
        locked.rejection_reason = reason.strip() if status == S.REJECTED else ""
        locked.decided_by = actor
        locked.decided_at = timezone.now()
        locked.save()
    return locked


def as_payload(req: CanonGapRequest) -> dict:
    """Форма наружу — одна на POST и GET."""
    return {
        "id": str(req.pk),
        "specialist_id": str(req.specialist_id),
        "name": req.name,
        "description": req.description,
        "duration_minutes": req.duration_minutes,
        "price": str(req.price),
        "status": req.status,
        "status_label": STATUS_LABELS[req.status],
        "resolved_template_id": str(req.resolved_template_id) if req.resolved_template_id else None,
        "clarification_question": req.clarification_question or None,
        "rejection_reason": req.rejection_reason or None,
        "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        "created_at": req.created_at.isoformat(),
    }
