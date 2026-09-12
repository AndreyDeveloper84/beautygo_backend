"""Стадии 2–3 — discovery и evidence. Recall, не approval.

Кандидат = канон, который какое-то правило *могло бы* выбрать, плюс
информативные совпадения. Решение принимает ``decide``; здесь только факты.
"""
from __future__ import annotations

from services.mapping.catalog import CatalogSnapshot, TemplateRef
from services.mapping.types import Candidate, EvidenceType, NormalizedEvidence, Rule


def _candidate(ref: TemplateRef, evidence: EvidenceType, rule: Rule | None) -> Candidate:
    return Candidate(
        template_id=ref.template_id, canonical_code=ref.canonical_code, name=ref.name,
        category_name=ref.category_name, evidence_type=evidence, lifecycle=ref.lifecycle,
        requires_health_check=ref.requires_health_check, rule=rule,
    )


def discover(ev: NormalizedEvidence, snap: CatalogSnapshot) -> tuple[list[Candidate], list[str]]:
    """Кандидаты + замечания discovery (например, синоним на два канона).

    * **R1 exact pair** — (norm категория салона, norm имя) ↔ каноны; правило
      требует ровно один канон *с кодом* — проверяет ``decide``;
    * **R2 approved synonym** — ``synonym.normalized == norm имя``; синоним
      всегда несёт провенанс (схема), «approved» здесь = ровно один канон за
      всеми такими синонимами;
    * **R0** — внешнего кода у услуги салона сегодня нет; правило объявлено,
      кандидатов не даёт;
    * **NAME_ONLY** — имя совпало в другой подкатегории; информативно.
    """
    candidates: list[Candidate] = []
    notes: list[str] = []
    seen: set = set()

    for tid in snap.by_pair.get((ev.norm_category, ev.norm_name), []):
        candidates.append(_candidate(snap.templates[tid], EvidenceType.EXACT_PAIR_RULE, Rule.R1))
        seen.add(tid)

    syns = snap.synonyms.get(ev.norm_name, [])
    targets = sorted({s.template_id for s in syns}, key=lambda t: (snap.templates[t].canonical_code or "", str(t)))
    if len(targets) > 1:
        notes.append("SYNONYM_AMBIGUOUS")
    for tid in targets:
        if tid in seen:
            continue                      # тот же канон уже найден по паре — не дублируем кандидата
        rule = Rule.R2 if len(targets) == 1 else None
        candidates.append(_candidate(snap.templates[tid], EvidenceType.APPROVED_ALIAS, rule))
        seen.add(tid)

    for tid in snap.by_name.get(ev.norm_name, []):
        if tid in seen:
            continue
        candidates.append(_candidate(snap.templates[tid], EvidenceType.NAME_ONLY, None))
        seen.add(tid)

    candidates.sort(key=lambda c: (c.canonical_code or "~", str(c.template_id)))
    return candidates, notes
