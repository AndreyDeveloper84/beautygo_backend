"""«Кому видно демо» — одно определение и его сторож (DRF-2420).

Тот же класс сторожа, что у ``test_sellable_predicate_1845``, и по той же
причине: условие «продаётся» однажды было записано семь раз, и четыре записи
из семи оказались неполными. Пулов, которым нужна видимость демо, ровно те же
пять, поэтому запрет пишется сразу, а не после первого расхождения.

Что заперто:

* **класс**: в не-тестовом коде нет сырых ``is_demo`` / ``is_test_persona``
  вне ``users/sellable.py`` и мест, где они ОБЪЯВЛЕНЫ (модели) либо
  ПРОСТАВЛЯЮТСЯ (команда пометки). Читать их напрямую — значит завести шестую
  копию правила;
* **перепись**: каждый из пяти пулов зовёт предикат. Нижняя граница списком, а
  не поиском: сторож, посмотревший не туда, не должен проходить на пустом
  результате.

Предел сторожа тот же: он читает написание, а не смысл. Алиас или фильтр из
переменных ему не видны.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

#: Сырое обращение к признакам. `is_test_persona(` — вызов предиката из
#: `users.sellable`, он разрешён и из шаблона исключён.
RAW = re.compile(
    r"(?:\w+__)?is_demo\s*[=)]|(?<!def )is_test_persona\s*=",
)

SKIP_PARTS = {"tests", "migrations", "commands", "seeds", "venv", ".venv", "node_modules"}

#: Где сырая форма законна, каждое — со своей причиной.
ALLOWED = {
    "users/sellable.py": "определение правила",
    "tenants/models.py": "объявление поля `Tenant.is_demo`",
    "users/models.py": "объявление поля `User.is_test_persona`",
}

#: Перепись пулов, обязанных ходить через предикат.
CENSUS = {
    "users/recommendation_source.py": "полки 1–2 подбора",
    "users/catalog_recommendations_api.py": "полка 3, счётчики категорий",
    "ai/application/services/recommendation_engine.py": "движок главной",
    "search/views.py": "глобальный поиск, три выборки",
    "users/specialists_api.py": "публичный список и карточка",
}


def _raw_hits() -> dict[str, int]:
    out: dict[str, int] = {}
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if set(rel.split("/")) & SKIP_PARTS:
            continue
        found = RAW.findall(path.read_text(encoding="utf-8", errors="ignore"))
        if found:
            out[rel] = len(found)
    return out


class TestTheClassGuard:
    def test_the_pattern_catches_every_spelling_it_names(self):
        """Сторож проверен на самом себе: иначе зелёный ничего не значит."""
        for sample in (
            "is_demo=False",
            "tenant__is_demo = False",
            "filter(is_demo)",
            "is_test_persona = True",
        ):
            assert RAW.search(sample), sample
        # Вызов предиката — не сырая форма.
        assert not RAW.search("if is_test_persona(viewer):")
        assert not RAW.search("demo_visibility_q(request.user)")

    def test_no_raw_flag_outside_the_definition(self):
        unexpected = {
            rel: count for rel, count in _raw_hits().items() if rel not in ALLOWED
        }

        assert unexpected == {}, (
            "видимость демо читается напрямую — это шестая копия правила; "
            f"зовите users.sellable.demo_visibility_q: {unexpected}"
        )

    def test_the_definition_itself_is_where_the_guard_expects_it(self):
        """Положительная стража: сторож смотрит в живое дерево, а не в пустоту."""
        hits = _raw_hits()

        assert hits.get("users/sellable.py", 0) >= 1
        # Каждое разрешённое место действительно существует и действительно
        # содержит признак: разрешение без строки — мёртвая запись, которая
        # прикроет чужой случай, когда файл появится.
        assert set(ALLOWED) <= set(hits)


class TestTheCensusCallsThePredicate:
    def test_every_pool_goes_through_the_one_definition(self):
        missing = {}
        for rel, what in CENSUS.items():
            text = (REPO / rel).read_text(encoding="utf-8")
            if "demo_visibility_q" not in text and "demo_scope_q" not in text:
                missing[rel] = what

        assert missing == {}, f"пул не зовёт предикат видимости демо: {missing}"
