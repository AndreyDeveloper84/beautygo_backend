"""Снимок канона и синонимов — читается один раз на прогон, дальше только память.

Резолвер не ходит в базу построчно: снимок делает прогон детерминированным
(одна и та же база → одни и те же строки) и позволяет parity-проверке
MAP-AUTO-06 сравнивать два прогона по одному входу.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from uuid import UUID

from services.canonical_code import SEED_PATH
from services.models import ServiceTemplate, ServiceTemplateSynonym
from services.normalization import normalize_service_name


@dataclass(frozen=True)
class TemplateRef:
    template_id: UUID
    canonical_code: str | None
    name: str
    category_name: str
    lifecycle: str
    requires_health_check: bool
    has_contraindications: bool


@dataclass(frozen=True)
class SynonymRef:
    template_id: UUID
    normalized: str
    confirmed_by_id: int | None
    confirmed_rule: str
    rule_version: str


@dataclass
class CatalogSnapshot:
    seed_version: str
    templates: dict[UUID, TemplateRef] = field(default_factory=dict)
    #: (norm category, norm name) → template ids
    by_pair: dict[tuple[str, str], list[UUID]] = field(default_factory=lambda: defaultdict(list))
    #: norm name → template ids (любая категория)
    by_name: dict[str, list[UUID]] = field(default_factory=lambda: defaultdict(list))
    #: synonym.normalized → синонимы
    synonyms: dict[str, list[SynonymRef]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def load(cls) -> "CatalogSnapshot":
        snap = cls(seed_version=SEED_PATH.name)
        for t in ServiceTemplate.objects.select_related("category").order_by("canonical_code", "name"):
            ref = TemplateRef(
                template_id=t.pk, canonical_code=t.canonical_code or None, name=t.name,
                category_name=t.category.name, lifecycle=t.lifecycle,
                requires_health_check=bool(t.requires_health_check),
                has_contraindications=bool((t.contraindications or "").strip()),
            )
            snap.templates[t.pk] = ref
            key = (normalize_service_name(t.category.name), normalize_service_name(t.name))
            snap.by_pair[key].append(t.pk)
            snap.by_name[key[1]].append(t.pk)
        for s in ServiceTemplateSynonym.objects.order_by("normalized", "template_id"):
            snap.synonyms[s.normalized].append(SynonymRef(
                template_id=s.template_id, normalized=s.normalized,
                confirmed_by_id=s.confirmed_by_id, confirmed_rule=s.confirmed_rule, rule_version=s.rule_version,
            ))
        return snap
