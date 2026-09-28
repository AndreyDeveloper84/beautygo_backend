"""DRF-2517 / DRF-2530 — боевой пилот остаётся защищённым, и список защищённых один.

Основание защиты ``formula-tela`` переписано (DRF-2517): защищается настоящий
прайс настоящего салона, а не «настоящие записи настоящих людей» — владелец
25.09: «в Формуле тела нет настоящих людей». Правка текстовая, и ровно поэтому
нужен сторож: исправление комментария не должно незаметно для ревью превратиться
в правку списка.

Список был определён трижды; DRF-2530 свёл его к одному модулю
``tenants/protected_slugs.py``.

Узлы держат:

* слаг стоит в каждом **импортируемом** наборе защищённых — у сида, у
  ``confirm_seeded_links``, у ``mark_demo_and_test_personas``, у
  ``bootstrap_tech_tenant``;
* все читатели держат **тот же объект** (``is``), а не равный: равенство прошло
  бы и при двух копиях;
* определение ``PROTECTED_SLUGS`` в исходниках **ровно одно** — в модуле-источнике.
  Охват печатается рядом с итогом, и «ноль» отличается от «лишних»: «ровно один»
  на пустом скане был бы пустым утверждением;
* копии множества под **другим** именем нет: литерал с пилотным слагом вне
  модуля-источника — та же копия, только поиск по имени её не видит;
* опровергнутая причина («настоящие записи настоящих людей») не возвращается
  ни в один исходник — однажды она уже расползлась в 13 мест.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PILOT = "formula-tela"
ROOT = Path(__file__).resolve().parents[2]
SOURCE = "tenants/protected_slugs.py"
#: Присваивание имени — в том числе с аннотацией (``PROTECTED_SLUGS: frozenset[str] = …``).
DEFINITION = re.compile(r"^\s*PROTECTED_SLUGS\s*(?::[^=\n]+)?=\s*(.+)$", re.MULTILINE)
#: Литерал МНОЖЕСТВА с пилотным слагом — копия под любым именем. Элемент множества
#: идёт за ``{``/``,`` и перед ``,``/``}``; ключ словаря (``"formula-tela": 35``,
#: счётчики по салонам) — перед ``:``, и это не копия списка.
LITERAL_COPY = re.compile(r'(?:frozenset\(\s*|=\s*)\{[^}:]*"formula-tela"\s*[,}]')


def _sources():
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith((".venv/", "venv/", "node_modules/")) or "/migrations/" in rel:
            continue
        yield rel, path


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


def test_every_reader_holds_the_same_object() -> None:
    from appointments.management.commands import bootstrap_tech_tenant
    from services.management.commands import confirm_seeded_links, seed_demo_salons
    from tenants import protected_slugs
    from tenants.management.commands import mark_demo_and_test_personas

    source = protected_slugs.PROTECTED_SLUGS
    readers = {
        "seed_demo_salons.PROTECTED_SLUGS": seed_demo_salons.PROTECTED_SLUGS,
        "confirm_seeded_links.PROTECTED_SLUGS": confirm_seeded_links.PROTECTED_SLUGS,
        "bootstrap_tech_tenant.PROTECTED_SLUGS": bootstrap_tech_tenant.PROTECTED_SLUGS,
        "mark_demo_and_test_personas.SEED_PROTECTED_SLUGS": (
            mark_demo_and_test_personas.SEED_PROTECTED_SLUGS
        ),
    }
    assert PILOT in source
    not_the_same = [name for name, value in readers.items() if value is not source]
    assert not_the_same == [], f"не тот же объект, что в {SOURCE}: {not_the_same}"


def _definitions() -> tuple[int, list[tuple[str, str]]]:
    scanned, found = 0, []
    for rel, path in _sources():
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in DEFINITION.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append((f"{rel}:{line}", match.group(1)))
    return scanned, found


def test_there_is_exactly_one_definition_and_it_keeps_the_pilot() -> None:
    scanned, definitions = _definitions()
    report = (
        f"просмотрено файлов: {scanned}; определений PROTECTED_SLUGS: "
        f"{len(definitions)} — {definitions}"
    )
    print(report)
    # Охват первым: ослепший обход дал бы «ноль», а не «один».
    assert scanned > 500, report
    # «Ноль» и «лишние» — разные поломки, и сообщение их различает.
    assert len(definitions) != 0, f"определение пропало (или имя собирается иначе): {report}"
    assert len(definitions) == 1, f"копия списка защищённых вернулась: {report}"
    where, value = definitions[0]
    assert where.startswith(SOURCE + ":"), report
    assert f'"{PILOT}"' in value, report


def test_no_copy_of_the_set_under_another_name() -> None:
    scanned, copies = 0, []
    for rel, path in _sources():
        if rel == SOURCE or "/tests/" in rel or rel.startswith("tests/"):
            continue
        scanned += 1
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in LITERAL_COPY.finditer(text):
            copies.append(f"{rel}:{text.count(chr(10), 0, match.start()) + 1}")
    # Охват: исходников вне тестов сегодня ~500; меньше 400 — обход ослеп.
    assert scanned > 400, scanned
    assert copies == [], f"литерал множества с {PILOT} вне {SOURCE}: {copies}"


#: Прежнее основание защиты — опровергнуто владельцем 25.09. Цитата в «ёлочках»
#: (как в ``tenants/protected_slugs.py``, где записано, что оно неверно) не
#: считается: это рассказ о старой причине, а не утверждение её.
OLD_REASON = re.compile(
    r"(?<!«)настоящ\w+ запис\w+ настоящих людей|напоминания живым людям", re.IGNORECASE
)
#: Положительная пара к ``OLD_REASON``: та же фраза, но в «ёлочках». Единственный
#: известный носитель — рассказ о старой причине у модуля-источника (DRF-2530
#: перенёс его туда из ``seed_demo_salons`` вместе с определением).
QUOTED_OLD_REASON = re.compile(r"«настоящ\w+ запис\w+ настоящих людей»", re.IGNORECASE)
QUOTED_CARRIER = SOURCE


def test_the_refuted_reason_does_not_come_back() -> None:
    """Неверная причина уже расползлась однажды — в 13 мест 8 файлов.

    Вернувшись в любой файл, она снова читается как факт и однажды снимет
    защиту чужими руками. Миграции не читаются: исторический текст там про
    другой предмет (каскад ``PROTECT`` при откате).
    """
    scanned, hits, quoted = 0, [], []
    for rel, path in _sources():
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
