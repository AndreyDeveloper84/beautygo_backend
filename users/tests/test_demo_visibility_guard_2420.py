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

#: Сырое обращение к признакам — ЛЮБОЕ упоминание имени, кроме вызова
#: предиката. Прежний шаблон требовал `=` или `)` после имени и потому не
#: видел ни `if tenant.is_demo:`, ни `filter(**{"tenant__is_demo": False})` —
#: а вторая форма это ровно та, которой пользуется САМО определение, то есть
#: копия писалась бы именно в невидимом написании (найдено ревью).
#:
#: Что НЕ считается копией и потому исключено: вызов предиката
#: `is_test_persona(...)`, его импорт, его объявление и упоминание пути к нему
#: в прозе (`users.sellable.is_test_persona`). Всё это — обращение к одному
#: определению. Без этих исключений сторож краснел на санкционированных местах
#: (`home_api` импортирует предикат, движок ссылается на него в комментарии).
RAW = re.compile(
    # Поле салона: любое упоминание. Имени `is_demo` нет ни в одном
    # санкционированном пути — `viewer_sees_demo` его не содержит.
    r"is_demo"
    # Признак личности: ЧТЕНИЕ поля (`viewer.is_test_persona`,
    # `is_test_persona = True`), но не вызов, не импорт, не объявление, не путь.
    r"|(?<!sellable\.)(?<!import )(?<!def )is_test_persona(?!\s*\()"
)

#: `.claude` — рабочие деревья. Без него сканер видит 28 вложенных копий
#: репозитория (9011 файлов, ~54 с), и СВОИ ЖЕ законные файлы становятся
#: «неожиданными» через префикс worktree: сторож красный на машине автора и
#: зелёный в CI, где свежий клон вложенных деревьев не имеет.
SKIP_PARTS = {
    "tests", "migrations", "commands", "seeds", "venv", ".venv",
    "node_modules", ".claude",
}

#: Где сырая форма законна, каждое — со своей причиной.
ALLOWED = {
    "users/sellable.py": "определение правила",
    "tenants/models.py": "объявление поля `Tenant.is_demo`",
    "users/models.py": "объявление поля `User.is_test_persona`",
    # Экран владельца: он ставит и снимает признак руками, значит поле обязано
    # быть ВИДНО. Это не копия правила видимости — здесь ничего не решают про
    # выдачу, здесь только показывают и правят само поле.
    "tenants/admin.py": "админка салона: признак виден и правится владельцем",
    "users/admin.py": "админка личности: там же и по той же причине",
}

#: Перепись пулов, обязанных ходить через предикат.
CENSUS = {
    "users/recommendation_source.py": "полки 1–2 подбора",
    "users/catalog_recommendations_api.py": "полка 3, счётчики категорий",
    "ai/application/services/recommendation_engine.py": "движок главной",
    "search/views.py": "глобальный поиск, три выборки",
    "users/specialists_api.py": "публичный список и карточка",
}


def _predicate_body() -> str:
    """Исходник тела `demo_scope_q` — без докстринга и без комментариев.

    Читается из живого файла и по живому объекту, чтобы «положительная
    стража» опиралась на код, который исполняется, а не на текст рядом с ним.
    """
    import inspect

    from users.sellable import demo_scope_q

    source = inspect.getsource(demo_scope_q)
    doc = demo_scope_q.__doc__ or ""
    if doc:
        source = source.replace(doc, "")
    return "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )


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
            # Формы, которые прежний шаблон пропускал, — первой та, которой
            # пользуется само определение.
            'filter(**{"tenant__is_demo": False})',
            "if tenant.is_demo:",
            "if not viewer.is_test_persona:",
            'getattr(viewer, "is_test_persona", False)',
            'annotate(d=F("tenant__is_demo"))',
            "[r for r in rows if not r.tenant.is_demo]",
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
        """Положительная стража, проверяемая УДАЛЕНИЕМ ТЕЛА, а не прозой.

        Прежняя версия требовала «хотя бы одно попадание в `users/sellable.py`»,
        и единственным попаданием там была строка ДОКСТРИНГА: удали тело
        предиката — зелено, удали одно предложение прозы — красно. То есть
        охранялась документация, а не поведение (найдено ревью). Теперь
        сторож смотрит на КОД предиката: имя поля внутри `demo_scope_q`.
        """
        body = _predicate_body()

        assert "is_demo" in body, (
            "в теле `demo_scope_q` нет имени поля — предикат не читает "
            "признак, и сторожить нечего"
        )
        # Каждое разрешённое место действительно существует и действительно
        # содержит признак: разрешение без строки — мёртвая запись, которая
        # прикроет чужой случай, когда файл появится.
        assert set(ALLOWED) <= set(_raw_hits())


class TestTheCensusCallsThePredicate:
    def test_every_pool_goes_through_the_one_definition(self):
        missing = {}
        for rel, what in CENSUS.items():
            text = (REPO / rel).read_text(encoding="utf-8")
            if "demo_visibility_q" not in text and "demo_scope_q" not in text:
                missing[rel] = what

        assert missing == {}, f"пул не зовёт предикат видимости демо: {missing}"
