"""`NOT_APPLICABLE` — решение владельца OD §72, контракт §4.1.

Проверяется не «состояние добавлено», а то, что оно **не может стать
обходом**. Владелец назвал два ограничения, и оба здесь стерегутся:

1. состояние определяется типом поверхности ДО решения, а не отсутствием
   данных — конструкция `payload.get("safety") or NOT_APPLICABLE`
   запрещена и ловится сторожем по исходникам;
2. заявление отменяется СОДЕРЖАНИЕМ решения: касается здоровья или
   персонализировано — значит fail-closed, как при `UNKNOWN`.
"""
from __future__ import annotations

import ast
from pathlib import Path

from recommendation.api import ReasonCode, SafetyState, resolve

from .conftest import StaticSource, make_facts, make_request

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ranked_ids(decision):
    return [c.candidate_ref.id for c in decision.ordered]


# ---------------------------------------------------------------------------
# Различие двух состояний
# ---------------------------------------------------------------------------

def test_not_applicable_admits_where_unknown_closes():
    """Два состояния — два поведения, и это вся суть решения владельца.

    `UNKNOWN` означает «оценка применима, данных нет» → fail-closed.
    `NOT_APPLICABLE` означает «поверхность не выполняет safety-sensitive
    решение» → гейт не применяется. Пустая полка не предотвращает
    опасного действия, если мастер и так доступен через каталог: она
    убирает объяснение, оставляя действие.
    """
    facts = make_facts()

    closed = resolve(
        make_request(safety_state=SafetyState.UNKNOWN), source=StaticSource([facts]),
    )
    opened = resolve(
        make_request(safety_state=SafetyState.NOT_APPLICABLE), source=StaticSource([facts]),
    )

    assert closed.is_empty
    assert _ranked_ids(opened) == [facts.ref.id]


def test_not_applicable_does_not_claim_safety_was_cleared():
    """Кода «безопасность пройдена» нет: проверки не было, очищать нечего.

    Разница между «проверили и чисто» и «не проверяли, потому что нечего
    проверять» обязана быть видна в кодах, иначе через месяц никто не
    отличит одно от другого — ровно как с рейтингом из сида.
    """
    decision = resolve(
        make_request(safety_state=SafetyState.NOT_APPLICABLE),
        source=StaticSource([make_facts()]),
    )
    assert ReasonCode.ELIG_SAFETY_CLEARED not in decision.ordered[0].reason_codes


# ---------------------------------------------------------------------------
# Инвариант против обхода — заявление отменяется содержанием
# ---------------------------------------------------------------------------

def test_health_check_candidate_refuses_the_declaration():
    """Витрина, в которой есть услуга с противопоказаниями, витриной не является.

    Заявление о неприменимости опровергается фактом, а не мнением
    о вызывающем: как только выдача касается здоровья, она обязана
    получить настоящий SafetyResult (OD §72).
    """
    decision = resolve(
        make_request(safety_state=SafetyState.NOT_APPLICABLE),
        source=StaticSource([make_facts(requires_health_check=True)]),
    )

    assert decision.is_empty
    assert [e.reason_code for e in decision.excluded] == [ReasonCode.ELIG_EXCLUDED_SAFETY]


def test_personalised_shelf_refuses_the_declaration():
    """«Тебе сейчас лучше вот эти» — уже интерпретация, а не витрина."""
    decision = resolve(
        make_request(safety_state=SafetyState.NOT_APPLICABLE),
        source=StaticSource([make_facts(prior_completed_visit=True)]),
    )

    assert decision.is_empty


def test_one_sensitive_candidate_closes_the_whole_answer():
    """Один кандидат с противопоказанием закрывает ВЕСЬ ответ.

    Не «показать остальных, спрятать этого»: отбор годных потребителем —
    та самая четвёртая власть, живущая в фильтре (§53.1). Здесь она была
    бы ещё и опаснее, потому что фильтровала бы по здоровью.
    """
    plain, sensitive = make_facts(), make_facts(requires_health_check=True)

    decision = resolve(
        make_request(safety_state=SafetyState.NOT_APPLICABLE),
        source=StaticSource([plain, sensitive]),
    )

    assert decision.is_empty


def test_declaration_still_works_on_a_plain_shelf():
    """Положительная стража: правило отменяет заявление НЕ всегда.

    Без неё три теста выше зеленели бы и на коде, который просто гасит
    NOT_APPLICABLE всегда — то есть на возврате к fail-closed.
    """
    decision = resolve(
        make_request(safety_state=SafetyState.NOT_APPLICABLE),
        source=StaticSource([make_facts(), make_facts()]),
    )
    assert len(decision.ordered) == 2


# ---------------------------------------------------------------------------
# Сторож против конструкции, названной владельцем поимённо
# ---------------------------------------------------------------------------

_SKIPPED = ("migrations", ".venv", "venv", "__pycache__", "node_modules", ".git", "tests")


def _mentions_not_applicable(node: ast.AST) -> bool:
    """Ссылается ли выражение на `NOT_APPLICABLE` — атрибутом или строкой."""
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "NOT_APPLICABLE":
            return True
        if isinstance(child, ast.Name) and child.id == "NOT_APPLICABLE":
            return True
        if isinstance(child, ast.Constant) and child.value == "NOT_APPLICABLE":
            return True
    return False


def _fallback_sites(tree: ast.AST) -> list[int]:
    """Места, где состояние берётся из ОТСУТСТВИЯ данных.

    Две формы, обе дают одно и то же:

    * ``что-то or NOT_APPLICABLE`` — классический fallback;
    * ``default=NOT_APPLICABLE`` в объявлении поля схемы — то же самое,
      только его подставит фреймворк.

    Разбор идёт по дереву, а не по тексту: докстринг, ЦИТИРУЮЩИЙ
    запрещённую конструкцию, — это документация правила, а не его
    нарушение, и текстовый сторож их не различал.
    """
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            # Опасен только ПРАВЫЙ операнд: `x or NOT_APPLICABLE`.
            if _mentions_not_applicable(node.values[-1]):
                found.append(node.lineno)
        if isinstance(node, ast.keyword) and node.arg == "default":
            if _mentions_not_applicable(node.value):
                found.append(node.value.lineno)
    return found


def test_no_code_derives_not_applicable_from_missing_data():
    """`safety = payload.get("safety") or NOT_APPLICABLE` — fail-open дыра.

    Владелец назвал эту конструкцию поимённо. Состояние определяется
    ТИПОМ ПОВЕРХНОСТИ до выполнения решения; отсутствие данных не
    превращается в него никогда, иначе первый же забытый параметр
    открывает гейт вместо того, чтобы его закрыть.

    Сторож разбирает дерево, а не текст: первая его версия ловила
    собственный докстринг `SafetyState`, который эту конструкцию
    цитирует, — то есть путала объяснение правила с его нарушением.
    """
    offenders = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in _SKIPPED for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "NOT_APPLICABLE" not in text:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        offenders.extend(f"{rel}:{line}" for line in _fallback_sites(tree))

    assert not offenders, (
        "состояние безопасности выведено из ОТСУТСТВИЯ данных (OD §72, контракт §4.1): "
        + ", ".join(offenders)
        + ". NOT_APPLICABLE объявляется поверхностью до решения, а не подставляется, "
        "когда поле не пришло"
    )


def test_the_guard_catches_the_forbidden_shape():
    """Сторож обязан краснеть на подложенной конструкции.

    Без этой проверки предыдущий тест зеленел бы и на разборе, который
    не находит ничего никогда, — а такой сторож хуже отсутствующего:
    он создаёт уверенность.
    """
    tree = ast.parse("safety = payload.get('safety') or SafetyState.NOT_APPLICABLE\n")
    assert _fallback_sites(tree) == [1]

    tree = ast.parse("field = ChoiceField(choices=X, default=SafetyState.NOT_APPLICABLE)\n")
    assert _fallback_sites(tree) == [1]

    tree = ast.parse("state = SafetyState.NOT_APPLICABLE\n")
    assert _fallback_sites(tree) == []
