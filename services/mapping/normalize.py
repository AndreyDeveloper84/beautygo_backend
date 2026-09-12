"""Стадия 1 — нормализация и маркеры. Raw сохраняется рядом с norm.

Ключ сравнения — ``services.normalization.normalize_service_name`` (тот же,
которым сверяются синонимы): casefold, ё→е, кавычки, пробелы. Здесь ничего
не переизобретается.

Маркеры **не решают** — они блокируют auto и уходят в ``safety_handoff``:

* **composition** — состав по обе стороны ``+`` (голый ``+`` в «FERULIC PEEL
  C+» — не состав; план, прил. A), слова «комплекс», «программа», «курс»,
  «пакет», скобки с перечислением (запятая или ``+`` внутри скобок);
* **health** — «бол» (боль/болях/болезн…), «снятие боли», «лечеб», «при <слово>»
  (показание: «при отёчности», «при болях»);
* **marketing** — словарь **пуст**: причина ``MARKETING_WORDING`` есть в
  словаре приложения B, но какие слова считать маркетинговыми, владелец не
  называл. Словарь закрытый и именованный (``MARKETING_WORDS``), чтобы его
  можно было заполнить словом владельца, а не догадкой; тест держит ветку
  живой подменой словаря.
"""
from __future__ import annotations

import re

from services.mapping.types import Markers, NormalizedEvidence
from services.normalization import normalize_service_name

#: «слово + слово» — состав; «C+» — нет.
_PLUS_BETWEEN_WORDS = re.compile(r"[\w)]\s*\+\s*[\w(]", re.U)
_COMPOSITION_WORDS = ("комплекс", "программа", "курс", "пакет")
_PAREN_ENUMERATION = re.compile(r"\([^)]*[,+][^)]*\)")

_HEALTH_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("снятие боли", re.compile(r"снятие\s+бол", re.U)),
    ("бол", re.compile(r"\bбол", re.U)),
    ("лечеб", re.compile(r"\bлечеб", re.U)),
    ("при …", re.compile(r"\bпри\s+\w", re.U)),
)

#: Закрытый словарь маркетинговых слов — пуст до слова владельца.
MARKETING_WORDS: frozenset[str] = frozenset()


def _composition_markers(norm: str) -> tuple[str, ...]:
    found: list[str] = []
    if _PLUS_BETWEEN_WORDS.search(norm):
        found.append("+")
    for word in _COMPOSITION_WORDS:
        if re.search(rf"\b{word}", norm):
            found.append(word)
    if _PAREN_ENUMERATION.search(norm):
        found.append("(…, …)")
    return tuple(found)


def _health_markers(norm: str) -> tuple[str, ...]:
    found: list[str] = []
    for label, pat in _HEALTH_PATTERNS:
        if pat.search(norm):
            found.append(label)
    # «снятие боли» уже содержит «бол» — один маркер, не два
    if "снятие боли" in found and "бол" in found:
        found.remove("бол")
    return tuple(found)


def _marketing_markers(norm: str) -> tuple[str, ...]:
    return tuple(w for w in sorted(MARKETING_WORDS) if re.search(rf"\b{re.escape(w)}\b", norm))


def normalize(raw_name: str, raw_category: str) -> NormalizedEvidence:
    norm = normalize_service_name(raw_name)
    return NormalizedEvidence(
        raw_name=raw_name,
        norm_name=norm,
        raw_category=raw_category,
        norm_category=normalize_service_name(raw_category),
        markers=Markers(
            composition=_composition_markers(norm),
            health=_health_markers(norm),
            marketing=_marketing_markers(norm),
        ),
    )


def split_components(raw_name: str) -> tuple[str, ...]:
    """Части состава для ``safety_handoff.components`` — только текстом, без резолва."""
    text = raw_name
    inner = _PAREN_ENUMERATION.search(text)
    if inner:
        text = inner.group(0).strip("()")
    parts = [p.strip(" .") for p in re.split(r"\s*[+,]\s*", text) if p.strip(" .")]
    return tuple(parts) if len(parts) > 1 else ()
