"""Review-поверхность владельца (MAP-AUTO-05): что показать и что разрешено сделать.

Лежит ВНЕ ``services/mapping/``: тот пакет read-only по построению (AST-сторож),
а здесь — единственное место, где решение владельца становится записью.

Модуль — *чтение* отчёта dry-run и *решение* человека через ту же форму
§76, которой пользуется админка. Ничего не предзаполняется как approved:
действия ниже исполняются только над строками, которые владелец выделил
сам, и каждое либо пишет через форму с ``confirmed_by = владелец``, либо
отказывает с названной причиной. Кнопки «применить всё» нет: действие над
строкой, у которой не ровно один кандидат или стоят флаги состава/здоровья,
— отказ, не догадка.

Единственный писатель — ``SalonServiceAdminForm`` (как в
``verify_pilot_slice._write``); после неё — шаг 4 §93 (синоним).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.forms.models import model_to_dict
from django.utils import timezone

from services.mapping.store import StoredReport
from services.mapping.types import Decision
from services.models import SalonService, ServiceTemplate

TERMINAL = {SalonService.MappingStatus.VERIFIED, SalonService.MappingStatus.NOT_RECOMMENDABLE}
BLOCKING_FLAG_PREFIXES = ("composition:", "health:")


@dataclass(frozen=True)
class Outcome:
    """Исход действия по одной строке — сообщение владельцу и был ли write."""

    service_id: str
    name: str
    written: bool
    message: str


def evidence_text(row: dict | None, report: StoredReport | None) -> str:
    """Читаемый блок для формы/списка. «Нет прогона» — отдельное состояние."""
    if report is None:
        return "нет прогона резолвера для этого салона — запустите «Пересчитать резолвером (dry-run)»"
    if row is None:
        return f"в отчёте от {report.generated_at:%d.%m %H:%M} этой услуги нет — пересчитайте"
    lines = [
        f"{row['decision']} / {row['reason']}" + (f"   [{row['hint']}]" if row.get("hint") else ""),
        f"правило: {row.get('rule_id') or '—'} · версия {row['rule_version']} · seed {row['seed_version']} · "
        f"прогон {report.generated_at:%d.%m %H:%M} (правила: {report.rules})",
    ]
    if row.get("flags"):
        lines.append("флаги: " + ", ".join(row["flags"]))
    for c in row.get("candidates", []):
        auto = f" ← {c['rule']}" if c.get("rule") else ""
        lines.append(
            f"кандидат {c.get('canonical_code') or '—'} «{c['name']}» ({c['category_name']}) "
            f"{c['evidence_type']} lifecycle={c['lifecycle']} rhc={c['requires_health_check']}{auto}"
        )
    b = row.get("before") or {}
    lines.append(f"сейчас: {b.get('mapping_status')}, template={b.get('template_id') or '—'}")
    if row.get("provenance_plan"):
        p = row["provenance_plan"]
        lines.append(f"план apply (не исполняется отсюда): {p['confirmed_rule']} · {p['source_ref']}")
    sh = row.get("safety_handoff") or {}
    lines.append(f"safety: canonical_rhc={sh.get('canonical_rhc')} local_rhc={sh.get('local_rhc')} "
                 f"health_markers={list(sh.get('health_markers') or [])} composite={sh.get('is_composite')}")
    return "\n".join(lines)


def _write_through_form(service: SalonService, **changes) -> None:
    from services.admin import SalonServiceAdmin, SalonServiceAdminForm

    data = model_to_dict(service)
    data |= changes
    form = SalonServiceAdminForm(data=data, instance=service)
    if not form.is_valid():
        raise ValidationError(
            "; ".join(f"{field}: {', '.join(errs)}" for field, errs in form.errors.items())
        )
    with transaction.atomic():
        saved = form.save()
        SalonServiceAdmin._record_salon_wording_as_synonym(saved)


def _report_ref(report: StoredReport, row: dict) -> str:
    return f"{report.path.name}@{report.generated_at:%Y-%m-%dT%H:%M} {row['decision']}/{row['reason']}"


def confirm_single_candidate(service: SalonService, who, today: date | None = None) -> Outcome:
    """«Подтвердить связь с каноном X»: X — единственный кандидат резолвера.

    Отказы (без записи): нет прогона / строки в отчёте; строка решена
    (VERIFIED / NOT_RECOMMENDABLE); кандидатов не ровно один; стоят флаги
    состава или здоровья (такое решает форма руками, с глазами); канон
    без кода или не approved. Провенанс — владелец: ``confirmed_by=who``,
    ``source_ref`` = «review <дата>: resolver …».
    """
    today = today or timezone.localdate()
    sid, name = str(service.pk), service.name
    if service.mapping_status in TERMINAL:
        return Outcome(sid, name, False, f"уже решено: {service.mapping_status} — не переигрывается")
    report = StoredReport.load(service.tenant.slug)
    row = report.row(service.pk) if report else None
    if row is None:
        return Outcome(sid, name, False, "нет строки в отчёте резолвера — сначала «Пересчитать резолвером (dry-run)»")
    if row["decision"] == Decision.SKIP_DECIDED.value:
        return Outcome(sid, name, False, "резолвер считает строку решённой — пересчитайте отчёт")
    blocking = [f for f in row.get("flags", []) if f.startswith(BLOCKING_FLAG_PREFIXES)]
    if blocking:
        return Outcome(sid, name, False, f"флаги {', '.join(blocking)} — только через форму, руками")
    cands = row.get("candidates", [])
    if len(cands) != 1:
        return Outcome(sid, name, False, f"кандидатов {len(cands)}, нужен ровно один — выберите канон на форме")
    cand = cands[0]
    if not cand.get("canonical_code"):
        return Outcome(sid, name, False, "у кандидата нет canonical_code — bootstrap не прошёл")
    if cand.get("lifecycle") != ServiceTemplate.Lifecycle.APPROVED:
        return Outcome(
            sid, name, False,
            f"канон {cand['canonical_code']} не approved ({cand['lifecycle']}) — сначала одобрить канон",
        )
    template = ServiceTemplate.objects.filter(canonical_code=cand["canonical_code"]).first()
    if template is None:
        return Outcome(sid, name, False, f"канон {cand['canonical_code']} не найден в базе (отчёт устарел?)")

    _write_through_form(
        service,
        template=template.pk,
        mapping_status=SalonService.MappingStatus.VERIFIED,
        mapping_confirmed_by=who.pk,
        mapping_confirmed_at=timezone.now(),
        mapping_confirmed_rule="",
        mapping_rule_version="",
        mapping_source_ref=f"review {today:%Y-%m-%d}: {_report_ref(report, row)} → {cand['canonical_code']}"[:200],
    )
    return Outcome(sid, name, True, f"VERIFIED → {cand['canonical_code']} «{cand['name']}», подтвердил {who.username}")


def mark_canon_gap(service: SalonService, who, today: date | None = None) -> Outcome:
    """«Канон-разрыв»: до модели CanonGapRequest (G6, M9 — ayla-7b) — отметка
    ``NOT_RECOMMENDABLE`` с ``source_ref`` «CANON_GAP: …», чтобы строку можно
    было найти и переиграть, когда модель появится. Это слово владельца,
    не резолвера: он лишь подсказывал ``POSSIBLE_CANON_GAP``."""
    today = today or timezone.localdate()
    sid, name = str(service.pk), service.name
    if service.mapping_status in TERMINAL:
        return Outcome(sid, name, False, f"уже решено: {service.mapping_status} — не переигрывается")
    report = StoredReport.load(service.tenant.slug)
    row = report.row(service.pk) if report else None
    ref = _report_ref(report, row) if row else "без отчёта резолвера"
    _write_through_form(
        service,
        mapping_status=SalonService.MappingStatus.NOT_RECOMMENDABLE,
        mapping_confirmed_by=who.pk,
        mapping_confirmed_at=timezone.now(),
        mapping_confirmed_rule="",
        mapping_rule_version="",
        mapping_source_ref=f"CANON_GAP: review {today:%Y-%m-%d}; {ref}"[:200],
    )
    return Outcome(sid, name, True, f"NOT_RECOMMENDABLE (CANON_GAP), отметил {who.username}")
