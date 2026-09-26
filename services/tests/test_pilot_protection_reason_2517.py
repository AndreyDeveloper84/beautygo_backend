"""DRF-2517 — боевой пилот остаётся защищённым, какой бы ни была формулировка.

Основание защиты ``formula-tela`` переписано: защищается настоящий прайс
настоящего салона, а не «настоящие записи настоящих людей» — владелец 25.09:
«в Формуле тела нет настоящих людей». Правка текстовая, и ровно поэтому нужен
сторож: исправление комментария не должно незаметно для ревью превратиться в
правку списка.

Узлы держат:

* слаг стоит в каждом **импортируемом** наборе защищённых — у сида, у
  ``confirm_seeded_links``, у ``mark_demo_and_test_personas``, у
  ``bootstrap_tech_tenant`` (последний — своя копия, не импорт);
* слаг стоит в **каждом определении** ``PROTECTED_SLUGS`` в исходниках — включая
  запасную копию в ``except ImportError`` у ``confirm_seeded_links``, которую
  импортом не достать, и любую копию, заведённую после этого листа. Число
  определений печатается, чтобы пустой обход не прошёл за «нарушений нет»;
* опровергнутая причина («настоящие записи настоящих людей») не возвращается
  ни в один исходник — однажды она уже расползлась в 13 мест.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PILOT = "formula-tela"
ROOT = Path(__file__).resolve().parents[2]
DEFINITION = re.compile(r"^\s*PROTECTED_SLUGS\s*=\s*(.+)$", re.MULTILINE)


@pytest.mark.parametrize(
    "module, name",
    [
        ("services.management.commands.seed_demo_salons", "PROTECTED_SLUGS"),
        ("services.management.commands.confirm_seeded_links", "PROTECTED_SLUGS"),
        ("tenants.management.commands.mark_demo_and_test_personas", "SEED_PROTECTED_SLUGS"),
        ("appointments.management.commands.bootstrap_tech_tenant", "PROTECTED_SLUGS"),
    ],
)
def test_the_pilot_stays_in_every_protected_set(module: str, name: str) -> None:
    import importlib

    protected = getattr(importlib.import_module(module), name)
    assert PILOT in protected, f"{module}.{name} = {sorted(protected)}"


def _definitions() -> list[tuple[str, str]]:
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith((".venv/", "venv/", "node_modules/")) or "/migrations/" in rel:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in DEFINITION.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append((f"{rel}:{line}", match.group(1)))
    return found


#: Прежнее основание защиты — опровергнуто владельцем 25.09. Цитата в «ёлочках»
#: (как у ``seed_demo_salons.PROTECTED_SLUGS``, где записано, что оно неверно)
#: не считается: это рассказ о старой причине, а не утверждение её.
OLD_REASON = re.compile(
    r"(?<!«)настоящ\w+ запис\w+ настоящих людей|напоминания живым людям", re.IGNORECASE
)
#: Положительная пара к ``OLD_REASON``: та же фраза, но в «ёлочках». Единственный
#: известный носитель — рассказ о старой причине у ``PROTECTED_SLUGS`` сида.
QUOTED_OLD_REASON = re.compile(r"«настоящ\w+ запис\w+ настоящих людей»", re.IGNORECASE)
QUOTED_CARRIER = "services/management/commands/seed_demo_salons.py"


def test_the_refuted_reason_does_not_come_back() -> None:
    """Неверная причина уже расползлась однажды — в 13 мест 8 файлов.

    Вернувшись в любой файл, она снова читается как факт и однажды снимет
    защиту чужими руками. Миграции не читаются: исторический текст там про
    другой предмет (каскад ``PROTECT`` при откате).
    """
    scanned, hits, quoted = 0, [], []
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith((".venv/", "venv/", "node_modules/")) or "/migrations/" in rel:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in OLD_REASON.finditer(text):
            hits.append(f"{rel}:{text.count(chr(10), 0, match.start()) + 1}")
        if QUOTED_OLD_REASON.search(text):
            quoted.append(rel)
    # Положительная пара — ПЕРЕД отрицательным утверждением и на тех же данных:
    # обход обязан найти известную цитату. Иначе «не нашёл запрещённое» прошло
    # бы и при ослепшем чтении (кодировка, не тот корень, сломанный шаблон).
    assert scanned > 500, scanned
    assert quoted == [QUOTED_CARRIER], quoted
    assert hits == [], hits


def test_every_source_definition_keeps_the_pilot() -> None:
    definitions = _definitions()
    # Охват: сегодня их три (сид, запасная копия confirm_seeded_links,
    # своя копия bootstrap_tech_tenant). Меньше трёх — обход ослеп или копию
    # удалили; и то и другое должно быть замечено, а не прочитано как «чисто».
    assert len(definitions) >= 3, definitions
    missing = [where for where, value in definitions if f'"{PILOT}"' not in value]
    assert missing == [], f"PROTECTED_SLUGS без {PILOT}: {missing} из {definitions}"
