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

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESOLVER_DIR = REPO_ROOT / "recommendation"

#: Вызовы, которые задают порядок. Сами по себе они невинны — запрещено не
#: сортировать, а сортировать **по признаку качества, близости или рейтинга**
#: (контракт §2.1 C1). Поэтому ищем в два шага: сначала вызов, потом признак
#: в его аргументах. Иначе гард ловил бы сортировку каталога по имени и был
#: бы отключён в первую же неделю — а отключённый сторож хуже отсутствующего.
_ORDERING_CALLS = re.compile(r"(order_by\(|\.sort\(|sorted\()")

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
    # Домашний экран Mini App. Ранжирование упраздняется целиком — T6.
    "users/catalog_recommendations_api.py": "DRF-1567 (T6): полки становятся проекциями resolve()",
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
    """
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
