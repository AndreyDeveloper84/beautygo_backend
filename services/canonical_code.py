"""Канонический код ``ServiceTemplate.canonical_code`` — контракт идентичности (OD-MAP-05).

Две идентичности у канонической услуги, и путать их нельзя:

* **``canonical_code``** — стабильная *логическая* идентичность строки
  эталонного справочника владельца (``1.3.24``). Она переживает переименование
  услуги, перенос в другую подкатегорию и пересоздание базы: по ней seed
  сверяется с базой, по ней задание владельцу называет канон, по ней будущий
  резолвер (MAP-AUTO-04) отличает «тот же канон» от «похожего по имени»;
* **``id``** — *технический* PK. Уникален в одной базе, не имеет смысла вне
  её, не сравнивается между базами и не печатается человеку.

Правила поля (MAP-AUTO-01):

* формат ``^\\d+\\.\\d+\\.\\d+$`` — все 1223 кода seed этой формы (``CODE_RE``);
* **уникальность** среди непустых (частичный ``UniqueConstraint``);
* **``NULL`` допустим и означает «канон не из эталонного списка»**: сорок
  строк ``seed_service_templates`` (DRF-196) и ``PROVISIONAL``-каноны,
  заведённые оператором (§93), кода не имеют и не получают;
* **неизменяемость после установки**: код, однажды записанный, не меняется ни
  формой, ни ``save()`` — только пустой можно заполнить. Смена кода = другая
  строка справочника, а не правка этой;
* код **не выводится** из имени или категории в ``save()`` — его источник
  только seed (bootstrap ``0023``, далее MAP-AUTO-03).

Bootstrap (``0023``, WP-02 (a)): ключ ``(category.name, name)`` → ``code`` из
seed-файла, **только** для строк с пустым кодом, **fail-closed**: любая
неоднозначность — отказ без единой записи и список причин. Функция ниже —
общая для миграции и тестов: миграция передаёт исторические модели через
``apps.get_model``, тесты — живые.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from django.core.exceptions import ValidationError

CODE_RE = re.compile(r"^\d+\.\d+\.\d+$")

SEED_PATH = Path(__file__).resolve().parent / "seeds" / "canonical_catalog_2026-07.json"


def validate_canonical_code(value: str) -> None:
    if value is None or value == "":
        return
    if not CODE_RE.fullmatch(value):
        raise ValidationError(
            f"canonical_code «{value}» не вида N.N.N — коды справочника владельца только такой формы",
            code="canonical_code_format",
        )


def seed_pairs(seed_path: Path = SEED_PATH) -> dict[tuple[str, str], str]:
    """``(имя подкатегории или категории, название) → код``.

    Ключ строится ровно так, как ``seed_canonical_catalog`` кладёт строку в
    базу (:118-129): категория шаблона — подкатегория, если она есть, иначе
    корневая. Сам seed обязан быть биекцией: дубль кода или дубль пары —
    ``BootstrapRefused`` до любого чтения базы.
    """
    rows = json.loads(seed_path.read_text(encoding="utf-8"))
    pairs: dict[tuple[str, str], str] = {}
    codes: dict[str, tuple[str, str]] = {}
    problems: list[str] = []
    for row in rows:
        code = str(row["code"]).strip()
        pair = ((row.get("subcategory") or row["category"]).strip(), str(row["service"]).strip())
        if not CODE_RE.fullmatch(code):
            problems.append(f"seed: код «{code}» не вида N.N.N")
        if code in codes:
            problems.append(f"seed: код {code} повторяется — {codes[code]} и {pair}")
        if pair in pairs:
            problems.append(f"seed: пара {pair} повторяется — коды {pairs[pair]} и {code}")
        codes[code] = pair
        pairs[pair] = code
    if problems:
        raise BootstrapRefused(problems)
    return pairs


class BootstrapRefused(RuntimeError):
    """Bootstrap не выполнен; ``problems`` — что именно неоднозначно."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("bootstrap canonical_code отказан:\n  " + "\n  ".join(problems))


@dataclass
class BootstrapReport:
    seed_pairs: int = 0
    templates_total: int = 0
    assigned: int = 0                      #: строк, которым код поставлен (или был бы — dry_run)
    already_coded: int = 0                 #: код уже стоит и совпадает с seed
    foreign: int = 0                       #: строк без кода, чьей пары в seed нет — остаются NULL
    missing_in_db: int = 0                 #: пар seed, которых в базе нет (база не засеяна целиком)
    assignments: list[tuple[str, tuple[str, str]]] = field(default_factory=list)

    def lines(self) -> list[str]:
        return [
            f"seed пар: {self.seed_pairs} · шаблонов в базе: {self.templates_total}",
            f"код поставлен: {self.assigned} · уже с кодом: {self.already_coded} · "
            f"чужих (остаются NULL): {self.foreign} · пар seed нет в базе: {self.missing_in_db}",
        ]


def bootstrap_canonical_codes(
    ServiceTemplate, *, seed_path: Path = SEED_PATH, dry_run: bool = False,
) -> BootstrapReport:
    """Поставить коды строкам без кода по паре ``(category.name, name)`` из seed.

    Fail-closed — отказ (``BootstrapRefused``) **до первой записи**, если:

    * seed не биекция (см. ``seed_pairs``);
    * строка базы уже несёт код, а seed для её пары называет другой;
    * два разных кода претендуют на одну строку базы или один код — на две
      (при ``unique_together(category, name)`` это возможно только при
      расхождении seed и базы, и всё равно проверяется, а не предполагается).

    «Пары seed нет в базе» — **не отказ**: пустая или частично засеянная база
    (CI, тестовая фикстура, база с чужими 40 шаблонами DRF-196) законна;
    такие пары считаются в ``missing_in_db`` и печатаются числом.
    Запись — одним ``bulk_update`` внутри транзакции вызывающего.
    """
    pairs = seed_pairs(seed_path)
    report = BootstrapReport(seed_pairs=len(pairs))
    rows = list(ServiceTemplate.objects.select_related("category").order_by("pk"))
    report.templates_total = len(rows)

    problems: list[str] = []
    claimed: dict[str, int] = {}
    to_update = []
    seen_pairs: set[tuple[str, str]] = set()
    for tpl in rows:
        pair = (tpl.category.name.strip(), tpl.name.strip())
        seen_pairs.add(pair)
        code = pairs.get(pair)
        current = tpl.canonical_code or None
        if code is None:
            if current is None:
                report.foreign += 1
            # строка с кодом, которого нет в seed, — не предмет bootstrap;
            # MAP-AUTO-03 назовёт её `code_reused` / `missing_in_seed`
            continue
        if current is not None and current != code:
            problems.append(f"строка {tpl.pk} {pair}: в базе код {current}, seed говорит {code}")
            continue
        if code in claimed:
            problems.append(f"код {code} претендует на две строки: {claimed[code]} и {tpl.pk}")
            continue
        claimed[code] = tpl.pk
        if current == code:
            report.already_coded += 1
            continue
        tpl.canonical_code = code
        to_update.append(tpl)
        report.assignments.append((code, pair))

    report.missing_in_db = sum(1 for p in pairs if p not in seen_pairs)
    if problems:
        raise BootstrapRefused(problems)
    report.assigned = len(to_update)
    if not dry_run and to_update:
        ServiceTemplate.objects.bulk_update(to_update, ["canonical_code"])
    return report


def unbootstrap_canonical_codes(ServiceTemplate, *, seed_path: Path = SEED_PATH) -> int:
    """Обратный ход ``0023``: обнулить **только** коды из seed — не «все»."""
    codes = set(seed_pairs(seed_path).values())
    return ServiceTemplate.objects.filter(canonical_code__in=codes).update(canonical_code=None)
