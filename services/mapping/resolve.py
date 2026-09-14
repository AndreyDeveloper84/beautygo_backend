"""Прогон по тенанту: снимок → по строке салона → ``ResolverRow``. Ничего не пишет."""
from __future__ import annotations

from services.mapping.catalog import CatalogSnapshot
from services.mapping.decide import RowInput, decide
from services.mapping.discover import discover
from services.mapping.normalize import normalize
from services.mapping.schema import SchemaNotReady, assert_schema_ready
from services.mapping.types import ResolverRow, RulesEnabled
from services.models import SalonService


def resolve_tenant(tenant, rules: RulesEnabled, *, report_ref: str = "dry-run") -> list[ResolverRow]:
    """Одна строка на каждую ``SalonService`` тенанта (активную и нет — обе
    считаются: неактивная тоже может быть решённой или спорной).

    Схема проверяется первой (``SchemaNotReady`` — стоп до чтения корпуса,
    план §10 «stale schema»).
    """
    assert_schema_ready()
    snap = CatalogSnapshot.load()
    rows: list[ResolverRow] = []
    services = (
        SalonService.objects.filter(tenant=tenant)
        .select_related("category", "category__parent")
        .order_by("name", "pk")
    )
    for n, svc in enumerate(services, start=1):
        category_name = svc.category.name if svc.category_id else ""
        ev = normalize(svc.name, category_name)
        candidates, notes = discover(ev, snap)
        inp = RowInput(
            salon_service_id=svc.pk,
            mapping_status=svc.mapping_status,
            template_id=svc.template_id,
            local_rhc=svc.requires_health_check,
            evidence=ev,
            candidates=tuple(candidates),
            discovery_notes=tuple(notes),
        )
        rows.append(decide(inp, rules, seed_version=snap.seed_version, report_ref=f"{report_ref}#{n}"))
    return rows


__all__ = ["resolve_tenant", "SchemaNotReady"]
