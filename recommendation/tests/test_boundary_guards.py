"""Сторожа границы — W1–W4 плюс структурные проверки. Задача T4 (DRF-1565).

Отличие этих тестов от остальных: они проверяют не поведение резолвера,
а **свойства репозитория**. Поведение можно починить правкой; свойство
репозитория — единственное, что мешает написать четвёртую формулу рядом.

Главный из них — :func:`test_no_ranking_outside_the_resolver`. Его ценность
не в том, что он вычистит старое (это делают миграции T6/T9/T12), а в том,
что он делает **новое** место ранжирования красным билдом уже сегодня, пока
не мигрировал ни один потребитель. Именно этого не хватало, чтобы вариант B
не выродился в «оставили как было».

Сторож уже оправдался: при постановке он нашёл **четвёртое** место
ранжирования, которого нет в аудите — `search/views.py` (DRF-1575).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from recommendation._authority import (
    NON_RANKING_CONSUMERS,
    RANKED_OUTPUT_CONSUMERS,
    RANKING_COMPONENTS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOLVER_DIR = REPO_ROOT / "recommendation"

#: Вызовы, которые задают порядок. Сами по себе они невинны — запрещено не
#: сортировать, а сортировать **по признаку качества, близости или рейтинга**
#: (контракт §2.1 C1). Поэтому ищем в два шага: сначала вызов, потом признак
#: в его аргументах. Иначе гард ловил бы сортировку каталога по имени и был
#: бы отключён в первую же неделю — а отключённый сторож хуже отсутствующего.
_ORDERING_CALLS = re.compile(r"(order_by\(|\.sort\(|sorted\()")

#: Те же вызовы для разбора по дереву. Имя вызываемого, а не подстрока:
#: `sort_order(...)` мимо, `qs.order_by(...)` и `sorted(...)` — сюда.
_ORDERING_NAMES = {"order_by", "sort", "sorted"}

#: Признак, по которому упорядочивать нельзя. `sort_order` и `sorted` мимо:
#: граница — на слове целиком.
_QUALITY_SIGNAL = re.compile(
    r"\b(rating|reviews_count|review_count|score|distance|haversine|proximity|km)\b"
)

#: Сколько символов после начала вызова считать его аргументами. Лямбда
#: со скобками не даёт обойтись балансировкой скобок регуляркой, а окно
#: длиннее начинает цеплять соседний код и давать ложные срабатывания.
_ARGS_WINDOW = 160

#: Каталоги, которые гард не смотрит. Тесты исключены сознательно: порядок,
#: собранный в тесте, — это фикстура, а не политика продукта.
_SKIPPED_PARTS = ("migrations", "tests", ".venv", "venv", "__pycache__", "node_modules", ".git")

#: Известные места, где ранжирование ещё живёт. У КАЖДОГО — задача, которая
#: его снимает. Исключение уходит вместе с правкой; пустой список = закрытая
#: граница. Список намеренно точечный (файл, а не каталог): каталог прикрыл
#: бы и то, что появится в нём завтра.
_ALLOWED = {
    # Домашний экран Mini App (DRF-1567, T6) отсюда УШЁЛ — ранжирования
    # в файле больше нет, и это первая строка реестра авторитетов,
    # сменившая «авторитет» на «удалено» не на словах, а тем, что гард
    # перестал нуждаться в исключении. Пока сторож был текстовым,
    # снять исключение было нельзя: файл продолжал «нарушать» цитатой
    # в собственном докстринге, где перечислено удалённое.
    # Движок LLM-контекста. Единая сумма 30/25/20/15/10 упраздняется — T9.
    "ai/application/services/recommendation_engine.py": "DRF-1570 (T9): компоненты переезжают по стадиям",
    # Поиск специалистов: рейтинг с отсечением + расстояние сортировкой.
    # Найдено этим же гардом, в аудите отсутствует — T13.
    "search/views.py": "DRF-1575 (T13): поиск — каталог или рекомендация, решает владелец",
    # Отзывы. НЕ кандидаты рекомендации: это список отзывов одного мастера,
    # и порядок задан человеком явно (?sort=rating). Исключение постоянное,
    # задачи под ним нет и не должно быть.
    "reviews/views.py": "не кандидаты рекомендации: порядок отзывов, заданный человеком явно",
}


def _python_files():
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if any(part in _SKIPPED_PARTS for part in path.parts):
            continue
        if rel.startswith("recommendation/"):
            continue
        yield rel, path


def _ranking_sites(text: str) -> list[tuple[int, str]]:
    """Места, где порядок задаётся по запрещённому признаку.

    Возвращает пары (строка, фрагмент). Пусто — значит файл сортирует
    по чему-то другому: по имени, по дате, по порядку в каталоге. Это
    разрешено и всегда было разрешено; запрещено ставить выше «лучшего».

    **Разбор идёт по дереву, а не по тексту.** Текстовый сторож не
    различал код и рассказ о коде: файл, у которого ранжирование
    УДАЛЕНО, но в докстринге названо удалённым, продолжал считаться
    нарушителем. Последствие тоньше ложного срабатывания: пока такой
    файл «нарушает», его исключение в :data:`_ALLOWED` выглядит живым
    и :func:`test_allowances_are_not_stale` молчит — то есть механизм,
    которым список исключений остаётся списком долгов, отключается
    ровно в тот момент, когда долг возвращён.

    Тот же приём уже применён сторожем §72 (`test_safety_not_applicable`)
    по той же причине: докстринг, ЦИТИРУЮЩИЙ запрещённую конструкцию, —
    это документация правила, а не его нарушение.

    Чего разбор по дереву по-прежнему не видит: порядок, собранный из
    переменной (``qs.order_by(*fields)``) или в другом файле. Это не
    анализ потока, и выдавать его за полный нельзя.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # Неразбираемый файл не должен становиться молча разрешённым:
        # падаем на прежнюю текстовую проверку. Она груба, но её грубость
        # в сторону «покраснеть», а не «промолчать».
        return _ranking_sites_textual(text)

    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node)
        if name not in _ORDERING_NAMES:
            continue
        args_src = ", ".join(
            [ast.unparse(arg) for arg in node.args]
            + [ast.unparse(kw) for kw in node.keywords]
        )
        signal = _QUALITY_SIGNAL.search(args_src)
        if signal:
            found.append((node.lineno, f"{name}(… {signal.group(0)}"))
    return found


def _called_name(node: ast.Call) -> str | None:
    """Имя вызываемого — `qs.order_by(...)` и `sorted(...)` одинаково."""
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _ranking_sites_textual(text: str) -> list[tuple[int, str]]:
    """Прежняя проверка — только как запасной путь для неразбираемого файла."""
    found = []
    for call in _ORDERING_CALLS.finditer(text):
        args = text[call.start(): call.start() + _ARGS_WINDOW]
        signal = _QUALITY_SIGNAL.search(args)
        if signal:
            line = text[: call.start()].count("\n") + 1
            found.append((line, f"{call.group(1)}… {signal.group(0)}"))
    return found


def test_no_ranking_outside_the_resolver():
    """W2: формула порядка живёт в одном месте — или билд красный.

    Контракт §2.1 C1 называет эту проверку сам: «в коде поверхностей не
    должно остаться ни одного `order_by` по признаку качества/близости/
    рейтинга над множеством кандидатов рекомендации».
    """
    offenders = []
    for rel, path in _python_files():
        if rel in _ALLOWED:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders.extend(f"{rel}:{line} — {fragment}" for line, fragment in _ranking_sites(text))

    assert not offenders, (
        "упорядочивание кандидатов вне резолвера (контракт §2.1 C1, OD §53):\n  "
        + "\n  ".join(offenders)
        + "\n\nЕсли это новая поверхность — она обязана звать recommendation.api.resolve, "
        "а не считать свой порядок. Если это НЕ кандидаты рекомендации — внесите файл "
        "в _ALLOWED с объяснением, почему, и это объяснение прочитают на ревью."
    )


def test_every_allowance_names_the_task_that_removes_it():
    """Исключение без задачи — это «временно», которое станет постоянным.

    Единственное исключение без задачи — постоянное и объяснено словами
    («не кандидаты рекомендации»). Всё остальное обязано ссылаться на DRF.
    """
    for path, reason in _ALLOWED.items():
        assert reason.startswith("DRF-") or "не кандидаты рекомендации" in reason, (
            f"{path}: исключение обязано либо называть задачу, которая его снимет, "
            f"либо объяснять, почему оно постоянное. Получено: {reason!r}"
        )


def test_allowances_still_exist():
    """Исключение на несуществующий файл — мусор, прикрывающий будущий файл."""
    for rel in _ALLOWED:
        assert (REPO_ROOT / rel).exists(), f"исключение {rel} ссылается на несуществующий файл"


def test_allowances_are_not_stale():
    """Исключение, которому больше нечего прикрывать, обязано уйти.

    Иначе список исключений перестаёт быть списком долгов и становится
    фоном, который никто не читает, — а вместе с ним перестаёт что-либо
    значить и правило «пустой список = закрытая граница».
    """
    stale = [
        rel for rel in _ALLOWED
        if not _ranking_sites((REPO_ROOT / rel).read_text(encoding="utf-8", errors="ignore"))
    ]
    assert not stale, (
        "исключения больше ничего не прикрывают — удалите их из _ALLOWED: " + ", ".join(stale)
    )


def test_the_guard_reads_code_and_not_prose_about_code():
    """Сторож обязан различать ранжирование и рассказ о нём.

    Проверка нужна ровно потому, что её отсутствие уже стоило одного
    отключённого механизма: пока разбор шёл по тексту, файл с удалённым
    ранжированием продолжал «нарушать» цитатой в собственном докстринге,
    его исключение выглядело живым, и `test_allowances_are_not_stale`
    молчал именно тогда, когда должен был заговорить.

    Обе половины обязательны. Без первой сторож ловит документацию;
    без второй — не ловит ничего, и зелень доказывает лишь то, что
    проверку ослабили.
    """
    prose = '"""Здесь стояло qs.order_by(\'-rating\', \'id\') — удалено (T6)."""\n'
    comment = '# было: sorted(items, key=lambda c: -c.rating)\nx = 1\n'
    assert _ranking_sites(prose) == []
    assert _ranking_sites(comment) == []

    assert _ranking_sites('qs.order_by("-rating", "id")\n')
    assert _ranking_sites('sorted(items, key=lambda c: -c.rating)\n')
    # Порядок по чему-то другому разрешён и всегда был разрешён.
    assert _ranking_sites('qs.order_by("-created_at")\n') == []


def test_unparseable_file_is_not_silently_allowed():
    """Запасной путь краснеет, а не молчит.

    Разбор по дереву мог бы стать способом обойти сторожа: файл,
    который не парсится, при `return []` оказался бы разрешён молча.
    """
    broken = 'def f(:\n    qs.order_by("-rating")\n'
    assert _ranking_sites(broken)


def test_nobody_outside_the_resolver_assembles_the_policy():
    """Политику собирает резолвер, а не поверхность — как и порядок.

    Стена W2 запрещает поверхности решать, КТО выше. Эта проверка
    запрещает ей решать, ПО КАКИМ ПРАВИЛАМ, и заведена не из симметрии:
    домашний экран уже собирал `StagePolicy` у себя, читая
    `RECOMMENDATION_PILOT_MAPPING_OVERRIDE`, а HTTP-проекция звала
    `resolve()` без политики и получала жёсткое умолчание. Одна политика
    имела два значения в одном процессе.

    Расхождение было невидимо ровно потому, что оба значения совпадали.
    Проявилось бы оно в момент включения флага — то есть тогда, когда
    на него уже перестали смотреть.
    """
    offenders = []
    for rel, path in _python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) == "StagePolicy":
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "политика собирается вне резолвера — у неё снова два значения:\n  "
        + "\n  ".join(offenders)
        + "\n\nЗовите resolve() без policy: умолчание читает настройки само "
        "(StagePolicy.from_settings). Явная политика допустима только в тестах."
    )


def test_no_setting_can_admit_an_unverified_mapping():
    """§76: пути, которым непроверенная связь попадает в подбор, не существует.

    Владелец: «ноль `VERIFIED` не разрешает fallback на
    `REVIEW_REQUIRED`, иначе статус будет декоративным, а система
    продолжит выдавать непроверенные связи».

    Запрет можно было исполнить, оставив флаг выключенным. Так делать
    нельзя: запрет, обходимый одной строкой в `settings`, — не запрет,
    и §74 называет порядок предпочтения прямо. Поэтому проверяется
    **отсутствие имени во всём репозитории**, а не его значение.

    Единственное разрешённое вхождение — комментарий, объясняющий, что
    настройка удалена намеренно; он оставлен, чтобы вернувшийся не решил,
    что её потеряли при рефакторинге, и не завёл заново. Разбор идёт по
    дереву: рассказ о запрещённом — не запрещённое (§74).
    """
    offenders = []
    for rel, path in _python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names.append(node.value)
            if any(n in _FORBIDDEN_ADMISSION_LEVERS for n in names):
                offenders.append(f"{rel}:{node.lineno}")

    assert not offenders, (
        "рычаг допуска в обход VERIFIED снова существует (§76):\n  "
        + "\n  ".join(offenders)
        + "\n\nСтатус связи подтверждается провенансом, а не настройкой."
    )


#: Имена, существование которых означает, что запрет владельца снова
#: обходится конфигурацией. Список короткий и должен таким остаться.
_FORBIDDEN_ADMISSION_LEVERS = frozenset({
    "RECOMMENDATION_PILOT_MAPPING_OVERRIDE",
    "mapping_override_enabled",
})


def test_private_modules_are_not_imported_from_outside():
    """Публичная поверхность — один модуль. Второй вход = второй авторитет."""
    pattern = re.compile(r"(from|import)\s+recommendation\._")
    offenders = []
    for rel, path in _python_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "импорт приватных модулей резолвера извне приложения: " + ", ".join(offenders)
        + ". Всё, что нужно снаружи, экспортирует recommendation.api"
    )


def test_stages_never_read_the_surface():
    """§9.5: один запрос — одно решение, независимо от поверхности.

    Поведенческий тест на это уже есть; структурный нужен потому, что
    поведенческий проверяет два значения `surface`, а этот — что читать
    его в стадиях нечем в принципе.
    """
    for name in ("_stages.py", "_pipeline.py"):
        text = (RESOLVER_DIR / name).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith(("#", "*", '"""', "'''"))
        )
        assert ".surface" not in code, f"{name} читает surface — стадии не вправе о нём знать (§9.5)"


def test_http_projection_gets_its_decision_through_the_public_surface():
    """Представление ходит через ту же дверь, что и все.

    Дай ему привилегию читать конвейер напрямую — и оно станет вторым
    входом в резолвер, через который пойдёт первый же «срочный фикс».
    """
    text = (RESOLVER_DIR / "views.py").read_text(encoding="utf-8")
    assert "from .api import" in text
    for private in ("._pipeline", "._stages", "._rotation", "._reason_codes"):
        assert private not in text, f"views.py импортирует {private} — решение берётся только через .api"


def test_no_display_string_fields_in_the_response_schema():
    """W1 на уровне схемы, а не одного ответа."""
    from recommendation._serializers import FORBIDDEN_RESPONSE_FIELDS, ResolveResponseSerializer

    def walk(serializer, seen=()):
        for name, field in serializer.fields.items():
            assert name not in FORBIDDEN_RESPONSE_FIELDS, f"поле {name} в схеме ответа границы"
            child = getattr(field, "child", None) or field
            if hasattr(child, "fields") and id(child) not in seen:
                walk(child, seen + (id(child),))

    walk(ResolveResponseSerializer())


def test_partially_conformant_response_is_invalid_as_a_whole():
    """W4 / §9.4.1: один битый элемент делает невалидным ОТВЕТ, а не элемент.

    Проверяется на серверной схеме: она обязана отвергать целиком, иначе
    «пропустить годные» окажется возможным просто потому, что схема это
    позволила. Клиентская половина — в `ai-bot-platform`.
    """
    from recommendation._serializers import ResolveResponseSerializer, decision_to_payload
    from recommendation.api import resolve

    from .conftest import StaticSource, make_facts, make_request

    payload = decision_to_payload(
        resolve(make_request(), source=StaticSource([make_facts(), make_facts(), make_facts()]))
    )
    assert ResolveResponseSerializer(data=payload).is_valid()

    payload["ordered"][1]["rank"] = "второй"
    serializer = ResolveResponseSerializer(data=payload)
    assert not serializer.is_valid(), "битый элемент обязан делать невалидным весь ответ"
    assert "ordered" in serializer.errors


# ---------------------------------------------------------------------------
# Вторая ось границы: кто берёт чужой порядок и выдаёт за решение (DRF-1628)
# ---------------------------------------------------------------------------
#
# Сторож выше ищет СИНТАКСИС упорядочивания. Он правдиво видит ноль в
# `users/home_api.py` и в `specialist_context_builder.py` — они и правда не
# сортируют. Они зовут того, кто сортирует, и отдают его порядок дальше как
# ответ Ayla. Такого потребителя проверка на `order_by` не видит по
# устройству, и ниже закрывается именно это.


def _importers_of(module_names: set[str]) -> dict[str, list[tuple[int, str]]]:
    """Кто импортирует названные модули — разбором дерева, не текстом.

    Текстом нельзя по той же причине, по которой её пришлось выучить
    выше: докстринг, называющий модуль, — рассказ о правиле, а не его
    нарушение. `_authority.py` целиком состоит из таких упоминаний.
    """
    found: dict[str, list[tuple[int, str]]] = {}
    for rel, path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            # Неразбираемый файл не становится молча разрешённым: он
            # уже роняет `test_unparseable_file_is_not_silently_allowed`.
            continue
        hits: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in module_names:
                hits.append((node.lineno, node.module))
            elif isinstance(node, ast.Import):
                hits.extend(
                    (node.lineno, alias.name)
                    for alias in node.names
                    if alias.name in module_names
                )
        if hits:
            found[rel] = hits
    return found


def test_every_consumer_of_a_ranking_component_is_declared():
    """Кто зовёт ранжировщика — назван и разобран, что именно берёт.

    Не «зовёт движок», а «берёт у движка порядок» либо «берёт предикат».
    Разница решающая: первое подменяет авторитет, второе нет. Молчаливый
    потребитель попадает сюда автоматически и остаётся красным, пока его
    не разберут словами.
    """
    declared = set(RANKED_OUTPUT_CONSUMERS) | set(NON_RANKING_CONSUMERS)
    undeclared = [
        f"{rel}:{lines[0][0]}"
        for rel, lines in _importers_of(set(RANKING_COMPONENTS)).items()
        if rel not in declared
    ]
    assert not undeclared, (
        "потребитель ранжирующего компонента не объявлен (DRF-1628):\n  "
        + "\n  ".join(undeclared)
        + "\n\nВнесите его в RANKED_OUTPUT_CONSUMERS, если он берёт ПОРЯДОК "
        "(тогда назовите задачу, которая это снимет), или в "
        "NON_RANKING_CONSUMERS, если берёт предикат либо счёт — "
        "и напишите, что именно."
    )


def test_every_ranked_output_consumer_names_the_task_that_removes_it():
    """Долг без задачи — это «временно», которое станет постоянным.

    У второй оси границы, в отличие от первой, постоянных исключений
    быть не может: взять чужой порядок и выдать за решение — всегда
    нарушение, вопрос только в сроке.
    """
    for path, reason in RANKED_OUTPUT_CONSUMERS.items():
        assert reason.startswith("DRF-"), (
            f"{path}: потребитель чужого порядка обязан называть задачу, "
            f"которая его снимет. Получено: {reason!r}"
        )


def test_declared_consumers_still_import_the_component():
    """Объявление на потребителя, который больше не зовёт, — мусор.

    Он прикрывает не долг, а пустоту, и вместе с ним перестаёт что-либо
    значить правило «пустой словарь = граница закрыта».
    """
    importers = set(_importers_of(set(RANKING_COMPONENTS)))
    stale = [
        rel for rel in (set(RANKED_OUTPUT_CONSUMERS) | set(NON_RANKING_CONSUMERS))
        if rel not in importers
    ]
    assert not stale, (
        "объявленные потребители больше не импортируют компонент — уберите: "
        + ", ".join(sorted(stale))
    )


def test_there_is_exactly_one_semantic_authority():
    """Ролей четыре, авторитет один.

    Если у второго компонента появится роль `SEMANTIC_RANKING`, вопрос
    «что Ayla рекомендует» получит два ответа, и выбирать между ними
    будет тот, кто первым попал в поверхность.
    """
    from recommendation._authority import ComponentRole, RANKING_COMPONENTS as _rc

    semantic = [name for name, role in _rc.items() if role is ComponentRole.SEMANTIC_RANKING]
    assert not semantic, (
        "компонент вне пакета резолвера объявлен семантическим авторитетом: "
        + ", ".join(semantic)
    )


def test_the_guard_does_not_fire_on_prose_about_the_component():
    """Сторож различает импорт и рассказ об импорте.

    `_authority.py` называет модуль движка трижды — в докстринге и в
    реестре строкой. Ни одно из упоминаний импортом не является, и
    файл обязан остаться чистым. Без этой проверки сторож поймал бы
    собственную документацию, как уже случалось с текстовой версией
    соседнего гарда.
    """
    assert "recommendation/_authority.py" not in _importers_of(set(RANKING_COMPONENTS))
