"""Транзакционный apply решений резолвера — ``AUTO_RULE`` (MAP-AUTO-06).

Лежит ВНЕ ``services/mapping/`` (тот пакет read-only по построению, AST-сторож):
здесь — единственное место, где решение **правила** становится записью.

Ворота закрыты
--------------

Первое действие — ``authorize_apply(tenant_slug=…)``. Сегодня она **всегда**
отказывает: OD-NEW-7 (apply authority) и OD-NEW-1/2 (правила R1/R2) владелец
не принял. Код ниже готов и доказан тестами на фикстурах с открытыми
воротами (подмена функции в тесте); команда в бою зовёт настоящую — и
пишет ноль строк. Открыть ворота = изменить ``authorize_apply`` по слову
владельца, а не этот модуль.

Что делает apply, по порядку (план §3 WP-06, §10)
-------------------------------------------------

1. **авторизация** — отказ до чтения корпуса;
2. **версии** — ``rule_version`` = версия кода правил, ``seed_version`` = файл
   seed снимка; расхождение с тем, что даёт резолвер сейчас, — стоп
   (``SEED_MISMATCH`` / правило поменялось между планом и apply);
3. **план** — dry-run резолвера (``resolve_tenant``) либо переданный план
   (например, отчёт, который владелец видел); к записи идут **только**
   ``AUTO_ELIGIBLE`` (ровно один auto-кандидат, approved, с кодом, правило
   включено, без флагов состава/здоровья);
4. **parity** — перед записью каждой строки резолвер пересчитывает её на
   живой базе; другое решение, другой канон или другой план провенанса —
   ``ParityMismatch``, стоп;
5. **идемпотентность** — уже ``VERIFIED`` с тем же каноном → пропуск;
   ``VERIFIED`` / ``NOT_RECOMMENDABLE`` иначе → резолвер отдаёт
   ``SKIP_DECIDED``, и строка к записи не попадает вовсе;
6. **запись** — через ``SalonServiceAdminForm`` (тот же писатель, что у
   админки и ``verify_pilot_slice``) + шаг 4 §93 (синоним); провенанс
   ``AUTO_RULE``: ``confirmed_by=None``, ``confirmed_rule=map_salon_services:R*``,
   ``rule_version``, ``confirmed_at=now``, ``source_ref=auto:R* seed=… report=…
   prev_template=…``. ``AUTO_RULE`` и ``OWNER`` различимы запросом по
   ``mapping_confirmed_rule != ""``;
7. **транзакция** — по умолчанию всё или ничего (``transaction.atomic`` на
   весь прогон); ``per_row=True`` — каждая строка своей транзакцией, провал
   строки не откатывает соседние и называется в отчёте;
8. **затронутые рёбра** — ``SpecialistService`` каждой записанной услуги:
   ``resolved_requires_health_check`` до и после (``None`` → ``bool``).

``requires_health_check`` самой услуги не трогается: unknown остаётся unknown.

Откат
-----

``rollback_auto_rule`` снимает **только** строки с
``mapping_confirmed_rule`` вида ``map_salon_services:*`` и данной
``rule_version`` → ``UNMAPPED`` + прежний шаблон из ``prev_template`` в
``source_ref``, и удаляет синонимы того же правила и версии у этого салона.
``VERIFIED`` владельца (``confirmed_by`` задан) не трогается никогда. Откат
идёт через те же ворота.
"""
from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from uuid import UUID

from django.db import transaction
from django.utils import timezone

from services.mapping import RulesEnabled, authorize_apply, resolve_tenant
from services.mapping.types import RULE_VERSION, Decision, ResolverRow
from services.mapping_review import _write_through_form
from services.models import SalonService, ServiceTemplate, ServiceTemplateSynonym, SpecialistService

AUTO_RULE_PREFIX = "map_salon_services:"
_PREV_TEMPLATE = re.compile(r"prev_template=([0-9a-f-]{36}|none)")
BLOCKING_FLAG_PREFIXES = ("composition:", "health:")


class ApplyStopped(RuntimeError):
    """Прогон остановлен до записи (или откатан целиком) — с названной причиной."""


class ParityMismatch(ApplyStopped):
    """План и живой пересчёт строки разошлись."""


@dataclass
class RowResult:
    salon_service_id: UUID
    name: str
    outcome: str            #: written | skipped | failed
    detail: str = ""


@dataclass
class ApplyReport:
    tenant_slug: str
    rule_version: str
    seed_version: str
    planned: int = 0                          #: строк в плане всего
    eligible: int = 0                         #: из них AUTO_ELIGIBLE (и в --only)
    rows: list[RowResult] = field(default_factory=list)
    edges_verdict_before: Counter = field(default_factory=Counter)
    edges_verdict_after: Counter = field(default_factory=Counter)

    @property
    def written(self) -> int:
        return sum(1 for r in self.rows if r.outcome == "written")

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.rows if r.outcome == "skipped")

    @property
    def failed(self) -> int:
        return sum(1 for r in self.rows if r.outcome == "failed")


def _verdict(edge: SpecialistService) -> str:
    return str(edge.resolved_requires_health_check())


def _edges(service_id: UUID) -> list[SpecialistService]:
    return list(
        SpecialistService.objects.filter(salon_service_id=service_id)
        .select_related("salon_service", "salon_service__template", "specialist")
    )


def _comparable(row: ResolverRow) -> tuple:
    """Что обязано совпасть между планом и живым пересчётом строки."""
    auto = [c for c in row.candidates if c.is_auto_candidate]
    plan = row.provenance_plan
    return (
        row.decision, row.reason, row.rule_id,
        tuple(str(c.template_id) for c in auto),
        (plan.confirmed_rule, plan.rule_version, _strip_report(plan.source_ref)) if plan else None,
    )


def _strip_report(source_ref: str) -> str:
    # report=…#n — ссылка на строку отчёта, у живого пересчёта своя; сравнивать правило/seed/prev_template
    return re.sub(r" report=\S+", "", source_ref)


def _fresh_row(tenant, rules: RulesEnabled, service_id: UUID, report_ref: str) -> ResolverRow | None:
    for r in resolve_tenant(tenant, rules, report_ref=report_ref):
        if r.salon_service_id == service_id:
            return r
    return None


def _apply_row(tenant, rules: RulesEnabled, planned: ResolverRow, report_ref: str,
               report: ApplyReport) -> RowResult:
    svc = SalonService.objects.select_for_update().get(pk=planned.salon_service_id)
    fresh = _fresh_row(tenant, rules, svc.pk, report_ref)
    if fresh is None or _comparable(fresh) != _comparable(planned):
        raise ParityMismatch(
            f"«{svc.name}»: план {_comparable(planned)} ≠ живой пересчёт "
            f"{_comparable(fresh) if fresh else 'строки нет'}"
        )
    blocking = [f for f in fresh.flags if f.startswith(BLOCKING_FLAG_PREFIXES)]
    if blocking:  # резолвер такого AUTO_ELIGIBLE не даёт; проверка — на случай его дефекта
        raise ApplyStopped(f"«{svc.name}»: флаги {blocking} у AUTO_ELIGIBLE — дефект резолвера, стоп")

    cand = next(c for c in fresh.candidates if c.is_auto_candidate)
    template = ServiceTemplate.objects.get(pk=cand.template_id)
    if svc.mapping_status == SalonService.MappingStatus.VERIFIED:
        if svc.template_id == template.pk:
            return RowResult(svc.pk, svc.name, "skipped", "уже VERIFIED с этим каноном")
        raise ApplyStopped(f"«{svc.name}»: VERIFIED с другим каноном — не переигрывается")

    edges = _edges(svc.pk)
    report.edges_verdict_before += Counter(_verdict(e) for e in edges)
    plan = fresh.provenance_plan
    _write_through_form(
        svc,
        template=template.pk,
        mapping_status=SalonService.MappingStatus.VERIFIED,
        mapping_confirmed_by=None,
        mapping_confirmed_at=timezone.now(),
        mapping_confirmed_rule=plan.confirmed_rule,
        mapping_rule_version=plan.rule_version,
        mapping_source_ref=plan.source_ref[:200],
    )
    report.edges_verdict_after += Counter(_verdict(e) for e in _edges(svc.pk))
    return RowResult(svc.pk, svc.name, "written", f"VERIFIED → {cand.canonical_code} ({plan.confirmed_rule})")


def apply_tenant(
    tenant,
    rules: RulesEnabled,
    *,
    rule_version: str,
    seed_version: str,
    report_ref: str = "apply",
    plan: Iterable[ResolverRow] | None = None,
    only: set[UUID] | None = None,
    per_row: bool = False,
    authorize: Callable[..., None] = authorize_apply,
) -> ApplyReport:
    authorize(tenant_slug=tenant.slug)          # ворота: сегодня всегда отказ

    if rule_version != RULE_VERSION:
        raise ApplyStopped(
            f"rule_version {rule_version!r} ≠ версии кода правил {RULE_VERSION!r} — план устарел"
        )
    rows = list(plan) if plan is not None else resolve_tenant(tenant, rules, report_ref=report_ref)
    report = ApplyReport(tenant.slug, rule_version, seed_version, planned=len(rows))
    seeds = {r.seed_version for r in rows}
    if rows and seeds != {seed_version}:
        raise ApplyStopped(f"SEED_MISMATCH: план на {sorted(seeds)}, apply на {seed_version!r}")

    only_ids = {str(x) for x in only} if only is not None else None
    eligible = [r for r in rows if r.decision is Decision.AUTO_ELIGIBLE
                and (only_ids is None or str(r.salon_service_id) in only_ids)]
    report.eligible = len(eligible)
    if not eligible:
        return report

    if per_row:
        for planned in eligible:
            try:
                with transaction.atomic():
                    report.rows.append(_apply_row(tenant, rules, planned, report_ref, report))
            except Exception as exc:  # noqa: BLE001 — строка названа в отчёте, соседи продолжают
                report.rows.append(RowResult(planned.salon_service_id, planned.raw_name, "failed", str(exc)))
        return report

    with transaction.atomic():
        for planned in eligible:
            report.rows.append(_apply_row(tenant, rules, planned, report_ref, report))
    return report


@dataclass
class RollbackReport:
    reverted: list[str] = field(default_factory=list)
    synonyms_removed: int = 0


def rollback_auto_rule(tenant, *, rule_version: str,
                       authorize: Callable[..., None] = authorize_apply) -> RollbackReport:
    """Снять записи правила данной версии у салона. Владельца не трогает."""
    authorize(tenant_slug=tenant.slug)
    report = RollbackReport()
    with transaction.atomic():
        rows = (
            SalonService.objects.select_for_update()
            .filter(
                tenant=tenant, mapping_status=SalonService.MappingStatus.VERIFIED,
                mapping_confirmed_by__isnull=True, mapping_confirmed_rule__startswith=AUTO_RULE_PREFIX,
                mapping_rule_version=rule_version,
            )
            .order_by("name", "pk")
        )
        for svc in rows:
            m = _PREV_TEMPLATE.search(svc.mapping_source_ref)
            prev = None if (m is None or m.group(1) == "none") else m.group(1)
            _write_through_form(
                svc,
                template=prev,
                mapping_status=SalonService.MappingStatus.UNMAPPED,
                mapping_confirmed_by=None,
                mapping_confirmed_at=None,
                mapping_confirmed_rule="",
                mapping_rule_version="",
                mapping_source_ref="",
            )
            report.reverted.append(svc.name)
        removed, _ = ServiceTemplateSynonym.objects.filter(
            source_tenant=tenant, confirmed_by__isnull=True,
            confirmed_rule__startswith=AUTO_RULE_PREFIX, rule_version=rule_version,
        ).delete()
        report.synonyms_removed = removed
    return report
