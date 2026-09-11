"""У ``ClientGoal`` нет ``is_active`` — и никто вне ``goals/`` не вправе его читать (DRF-1660).

### Какой класс дефекта здесь стережётся

#345 заменил ``ClientGoal.is_active`` шестью состояниями ``state``. Перед
слиянием читателей ``is_active`` вне ``goals/`` пересчитали — ноль. Между
замером и слиянием появился ``core/management/commands/surface_state.py``:
``Goal = apps.get_model("goals.ClientGoal")``, ``qs.filter(is_active=True)``.
Слияние прошло, команда падала ``FieldError`` — второй лист, #370.

Замер верен в момент замера и ни секундой позже. Этот тест превращает
замер в сторож: **любой** модуль вне ``goals/``, который берёт
``ClientGoal`` — импортом, через ``apps.get_model``, под псевдонимом — и
трогает ``is_active`` в любой форме (kwarg в filter/exclude/create/Q,
строка в values/order_by/only, атрибут на строке или переменной цикла),
называется здесь файлом и строкой.

### Чего сторож не видит — названо

* Читатель, который получает queryset/объект ``ClientGoal`` **параметром**
  функции (``def f(goal): goal.is_active``) — имя не привязано к модели в
  этом модуле, порча невидима. Такой читатель упадёт в рантайме на первом
  вызове; здесь его нет (перепись ниже).
* Читатель в чужом PR, поднятом ДО слияния этого сторожа: на ``pull_request``
  тесты бегут из головы PR, где сторожа ещё нет. Поймает прогон ``dev``
  после слияния — то есть тем же способом, что и #370, но тестом, а не
  командой на пилоте.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODEL_LABEL = "goals.ClientGoal"
MODEL_NAME = "ClientGoal"
FIELD = "is_active"
SKIP_DIRS = {"goals", ".venv", "venv", "node_modules", ".git"}


# ---------------------------------------------------------------------------
# сканер
# ---------------------------------------------------------------------------


def _root_name(node: ast.AST) -> str | None:
    """Имя в корне цепочки ``a.b(c).d[0]`` — или None, если корень не имя."""
    while True:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            node = node.value
        elif isinstance(node, ast.Call):
            node = node.func
        elif isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Await):
            node = node.value
        else:
            return None


def _is_get_model_of_clientgoal(node: ast.AST) -> bool:
    """``apps.get_model("goals.ClientGoal")`` / ``get_model("goals", "ClientGoal")``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != "get_model":
        return False
    consts = [a.value for a in node.args if isinstance(a, ast.Constant)]
    return MODEL_LABEL in consts or consts[-2:] == ["goals", MODEL_NAME]


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "goals.models":
            for alias in node.names:
                if alias.name == MODEL_NAME:
                    names.add(alias.asname or alias.name)
    return names


def _mentions_field(call: ast.Call) -> bool:
    """Внутри вызова есть ``is_active`` — kwarg, lookup, строка, Q(...)."""
    for sub in ast.walk(call):
        if isinstance(sub, ast.keyword) and sub.arg is not None:
            if sub.arg == FIELD or sub.arg.startswith(FIELD + "__"):
                return True
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            s = sub.value.lstrip("-")
            if s == FIELD or s.startswith(FIELD + "__"):
                return True
    return False


def _hits_in(node: ast.AST, names: set[str]) -> set[int]:
    hits: set[int] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call) and _root_name(sub) in names and _mentions_field(sub):
            hits.add(sub.lineno)
        elif (
            isinstance(sub, ast.Attribute)
            and sub.attr == FIELD
            and _root_name(sub.value) in names
        ):
            hits.add(sub.lineno)
    return hits


def _bindings(stmt: ast.stmt) -> tuple[list[str], ast.AST | None]:
    """Имена, которые оператор связывает, и выражение, из которого."""
    if isinstance(stmt, ast.Assign):
        return [t.id for t in stmt.targets if isinstance(t, ast.Name)], stmt.value
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        return ([stmt.target.id] if isinstance(stmt.target, ast.Name) else []), stmt.value
    if isinstance(stmt, ast.For):
        return ([stmt.target.id] if isinstance(stmt.target, ast.Name) else []), stmt.iter
    return [], None


def _scan_body(body: list[ast.stmt], names: set[str], hits: set[int]) -> None:
    """Пройти тело в порядке исходника, ведя множество имён за ClientGoal.

    Привязка — по потоку, не по модулю: ``qs = Tenant._base_manager.all()``
    в одном методе и ``qs = Goal._base_manager.all()`` в другом — разные
    ``qs``. Имя, переприсвоенное из чужого выражения, из множества уходит.
    Вложенные тела (функции, классы, ветки, циклы) получают копию.
    """
    names = set(names)
    for stmt in body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _scan_body(stmt.body, names, hits)
            continue
        targets, value = _bindings(stmt)
        if value is not None:
            hits |= _hits_in(value, names)
            tainted = _is_get_model_of_clientgoal(value) or _root_name(value) in names
            for t in targets:
                if tainted:
                    names.add(t)
                else:
                    names.discard(t)
        for _, val in ast.iter_fields(stmt):
            if isinstance(val, list) and val and isinstance(val[0], ast.stmt):
                _scan_body(val, names, hits)
            elif isinstance(val, list):
                for x in val:
                    if isinstance(x, ast.ExceptHandler):
                        _scan_body(x.body, names, hits)
                    elif isinstance(x, ast.AST) and not isinstance(x, ast.stmt):
                        hits |= _hits_in(x, names)
            elif isinstance(val, ast.AST) and val is not value:
                hits |= _hits_in(val, names)


def _is_active_readers(source: str) -> list[int]:
    """Строки, где привязанное к ClientGoal имя используется вместе с is_active."""
    tree = ast.parse(source)
    hits: set[int] = set()
    _scan_body(tree.body, _imported_names(tree), hits)
    return sorted(hits)


def _binds_clientgoal(source: str) -> bool:
    tree = ast.parse(source)
    if _imported_names(tree):
        return True
    return any(_is_get_model_of_clientgoal(n) for n in ast.walk(tree))


def _modules_outside_goals() -> list[Path]:
    out: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel.parts[0] in SKIP_DIRS or "migrations" in rel.parts:
            continue
        if "tests" in rel.parts or path.name.startswith("test_") or path.name == "conftest.py":
            continue
        out.append(path)
    return out


# ---------------------------------------------------------------------------
# сторож
# ---------------------------------------------------------------------------


def test_nobody_outside_goals_reads_clientgoal_is_active() -> None:
    modules = _modules_outside_goals()
    sources = {m: m.read_text(encoding="utf-8") for m in modules}
    # Перепись предмета — ДО «нарушителей нет». Пустая перепись значит
    # «искали не там» (неверный корень, сдвинутый фильтр), и квантор «ни
    # один» на пустом множестве прошёл бы зелёным.
    census = sorted(
        m.relative_to(REPO_ROOT).as_posix()
        for m, src in sources.items()
        if MODEL_NAME in src and _binds_clientgoal(src)
    )
    assert census, "ни одного модуля вне goals/ с привязкой к ClientGoal — измерялся не тот предмет"
    for expected in ("users/catalog_recommendations_api.py", "core/management/commands/surface_state.py"):
        assert expected in census, (expected, census)

    offenders = {
        m.relative_to(REPO_ROOT).as_posix(): lines
        for m, src in sources.items()
        if MODEL_NAME in src and (lines := _is_active_readers(src))
    }
    assert offenders == {}, (
        f"чтение ClientGoal.is_active вне goals/: {offenders}. Поля нет с #345 "
        "(DRF-1660) — есть state с шестью значениями; читать goals/lifecycle.py, "
        "не восстанавливать bool."
    )


# ---------------------------------------------------------------------------
# положительный контроль сканера — в обе стороны
# ---------------------------------------------------------------------------

IMPORT = "from goals.models import ClientGoal\n"


@pytest.mark.parametrize(
    ("snippet", "expected"),
    [
        # формы, которые обязаны ловиться
        (IMPORT + "ClientGoal.objects.filter(is_active=True)", [2]),
        (IMPORT + "ClientGoal.objects.filter(client=c).exclude(is_active=False)", [2]),
        (IMPORT + "ClientGoal.objects.create(client=c, is_active=True)", [2]),
        (IMPORT + "ClientGoal(client=c, is_active=True)", [2]),
        (IMPORT + "ClientGoal.objects.filter(Q(is_active=True) | Q(goal_key='x'))", [2]),
        (IMPORT + "ClientGoal.objects.filter(**{'is_active': True})", [2]),
        (IMPORT + "ClientGoal.objects.filter(is_active__isnull=True)", [2]),
        (IMPORT + "ClientGoal.objects.values('client', 'is_active')", [2]),
        (IMPORT + "ClientGoal.objects.order_by('-is_active')", [2]),
        (IMPORT + "g = ClientGoal.objects.get(pk=1)\nif g.is_active:\n    pass", [3]),
        (IMPORT + "for g in ClientGoal.objects.all():\n    print(g.is_active)", [3]),
        ("from goals.models import ClientGoal as CG\nCG.objects.filter(is_active=True)", [2]),
        # форма #370: get_model + queryset в переменной
        (
            "Goal = apps.get_model('goals.ClientGoal')\n"
            "qs = Goal._base_manager.all()\n"
            "n = qs.filter(is_active=True).count()",
            [3],
        ),
        ("M = apps.get_model('goals', 'ClientGoal')\nM.objects.filter(is_active=False)", [2]),
        # два метода, одно имя qs: Tenant.is_active живой, Goal — нет (форма surface_state)
        (
            "class C:\n"
            "    def a(self):\n"
            "        T = apps.get_model('tenants.Tenant')\n"
            "        qs = T._base_manager.all()\n"
            "        return qs.filter(is_active=True).count()\n"
            "    def b(self):\n"
            "        Goal = apps.get_model('goals.ClientGoal')\n"
            "        qs = Goal._base_manager.all()\n"
            "        return qs.filter(is_active=True).count()",
            [9],
        ),
        # формы, которые НЕ должны ловиться
        (
            "from goals.models import ClientGoal, GoalOption\n"
            "GoalOption.objects.filter(is_active=True)\n"
            "ClientGoal.objects.filter(state='active')",
            [],
        ),
        (IMPORT + "ClientGoal.objects.filter(client__is_active=True)", []),
        ("Other.objects.filter(is_active=True)", []),
        ("from goals.models import GoalOption\nGoalOption.objects.filter(is_active=True)", []),
    ],
)
def test_the_scanner_finds_every_form_and_ignores_other_models(snippet: str, expected: list[int]) -> None:
    """Без этого «нарушителей нет» неотличимо от «сканер ничего не умеет».

    Отрицательные случаи не менее важны: ``GoalOption.is_active`` — живое
    поле, и сторож, краснеющий на нём, отключат в первый же день.
    """
    assert _is_active_readers(snippet) == expected
