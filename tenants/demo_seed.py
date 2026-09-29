"""Файл сида демо-салонов — источник «какие УЖЕ заведённые салоны демонстрационные».

Код признак демонстрационности читает (``users.sellable.demo_scope_q``), а
список слагов здесь нужен ровно для строк, заведённых ДО поля (DRF-2420):
миграция ``tenants 0009`` проставила им всем ``False`` и тем слила «ещё не
решено» с «решено: не демо» (DRF-2646). Новые строки решаются при заведении:
сид ставит ``True`` сам, человек заводит с умолчанием ``False``.

Один читатель файла на команду пометки и на сторож ``tenants.W001`` — вторая
копия списка разошлась бы при первом же новом сиде.
"""

from __future__ import annotations

import json
from pathlib import Path

DEMO_SEED_FILE = (
    Path(__file__).resolve().parents[1] / "services" / "seeds" / "demo_salons_2026-08.json"
)


class DemoSeedUnreadable(ValueError):
    """Файла нет, он не JSON или в нём нет ни одного слага."""


def demo_seed_slugs(path: str | Path = DEMO_SEED_FILE) -> list[str]:
    """Слаги салонов файла сида — по порядку, без повторов."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise DemoSeedUnreadable(f"файл сида не найден: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DemoSeedUnreadable(f"файл сида не читается как JSON: {exc}") from exc
    slugs = [salon.get("slug") for salon in payload.get("salons", []) if salon.get("slug")]
    if not slugs:
        raise DemoSeedUnreadable(f"в файле сида нет слагов салонов: {path}")
    return list(dict.fromkeys(slugs))
