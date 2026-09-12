"""Сторож: ориентир не подставляется — ни числом, ни снятой формулой.

Зачем он важнее самой правки
----------------------------

Правка снимает сегодняшнюю фикцию. Сторож отвечает за завтрашнюю.

Место, где ориентира нет, читается как недоделка. Следующий добросовестный
человек — и это будет добросовестный человек, а не вредитель — увидит
``calories_goal`` без значения, найдёт «разумное умолчание» (2000 ккал,
восемь стаканов, 30 мл × вес) и вернёт его одной строкой, искренне считая,
что чинит. Ровно так восьмёрка стаканов возвращалась ДВАЖДЫ: сначала её
выкинули из клиента, а она приехала по проводу с нашей стороны; потом
сняли настройку — и ориентир стал читаться из анкеты, где лежал результат
той же самой формулы.

Поэтому сторож ловит не значение, а ФОРМУ возврата:

1. подстановку числа в поле-ориентир (``calories_goal = 2000``,
   ``water_goal_ml=2000``, ``... or 2000``);
2. чтение ``NutritionProfile.daily_water_ml`` — столбца, в котором у
   существующих клиентов лежит выход снятой формулы 30 мл × вес. Пока
   значение там, любое чтение эту формулу применяет, как бы место ни
   называлось. **Исключение с 11.09.2026 (§5.1, писатель ``user_entered``):**
   в тот же столбец пишется норма, названная человеком, и читать её
   можно — но ТОЛЬКО под условием происхождения. Сторож пропускает
   чтение, стоящее внутри ``if``, чей предикат называет ``USER_ENTERED``;
   чтение под любым другим условием или без него — по-прежнему нарушение.
   Признак — происхождение, а не значение: по числу 2100 не отличить
   «человек сказал» от «70 × 30». Запись в столбец (``= …``) и загрузка
   его в ``.only(...)`` чтением не считаются — читать значит использовать
   значение;
3. возврат самих имён ``WATER_ML_PER_KG`` и ``_water_target``.

Чего сторож НЕ ловит, и это надо знать
--------------------------------------

Переименование. Заведут ``daily_fluid_ml`` вместо ``daily_water_ml`` — и
список ниже протухнет молча. Лечится только тем, что новое имя допишут
сюда руками. Отдельно от этого стоит поведенческий замер
(``test_targets_absent.py``): он смотрит на ответ ручки и потому переживёт
любое переименование внутри.

Родственный сторож — ``test_no_invented_norms.py``: тот сторожит ИМЕНА
НАСТРОЕК (``NUTRITION_DEFAULT_*``), этот — подстановку значения в код.
Два разных способа вернуть одно и то же.

Замер 09.09.2026, снят ``python -m pytest
nutrition/tests/test_targets_stay_absent.py``: подставить
``calories_goal = 2000`` вместо ``calories_goal: int | None = None`` в
``nutrition_summary_service`` → ``1 failed, 3 passed``; вернуть как было
→ ``4 passed``. Подмена проверена на применение (ровно одно совпадение в
файле) — ``str.replace`` «успешен» и при нуле совпадений и доказал бы
свойство несуществующего кода.
"""
from __future__ import annotations

import ast
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]


#: Имена, несущие ОРИЕНТИР. Присвоить любому из них число — значит
#: вернуть выдуманную цель, как бы она ни называлась в тот день.
#:
#: Имён факта (``calories_total``, ``water_ml``, ``today_total_water_ml``)
#: здесь нет намеренно: ноль съеденного — настоящий ноль, и запрещать
#: его значило бы врать в другую сторону.
TARGET_NAMES = frozenset({
    "calories_goal",
    "water_goal_ml",
    "water_pct",
    "today_norm_water_ml",
    "today_progress_pct",
    "daily_water_ml",
})

#: Имена снятой методики. Пока они живы, вернуть 30 мл × вес — одна
#: строка (§82: формула «не используется без отдельно утверждённой
#: методики»).
REMOVED_FORMULA_NAMES = frozenset({
    "WATER_ML_PER_KG",
    "_water_target",
})

#: Столбец, в котором у существующих клиентов лежит выход снятой формулы.
#: Миграция данных — отдельный срез; до неё чтение запрещено.
STALE_COLUMN = "daily_water_ml"


#: Что разбирается. Сервисы питания, сериализаторы (граница наружу) и
#: рассылка: пуш уезжает человеку САМ, без запроса, поэтому под честное
#: слово комментария его оставлять нельзя.
def _scanned() -> tuple[Path, ...]:
    return (
        *sorted((_ROOT / "nutrition" / "services").glob("*.py")),
        _ROOT / "nutrition" / "serializers.py",
        _ROOT / "notifications" / "tasks.py",
    )


def _substitutions(source: str) -> list[str]:
    """Места, где ориентиру подставляют значение вместо отсутствия.

    Ловит четыре формы, все четыре встречались в этом коде живьём:

    * ``calories_goal = 2000`` — присваивание числа;
    * ``WaterAggregate(water_goal_ml=2000)`` — число в аргументе;
    * ``profile.daily_water_ml or 2000`` — «умолчание» через ``or``;
    * ``int(body.get("water_goal_ml") or 2000)`` — то же под слоем.

    ``None`` и ноль не считаются подстановкой: ``None`` — это и есть
    отсутствие, а ноль остаётся допустимым ВНУТРИ модулей, где он давно
    читается как «нет» (например ``_profile_goal_water`` возвращает 0.0).
    Запрещено класть в ориентир ЧИСЛО, а не объявлять его пустым.
    """
    found: list[str] = []
    tree = ast.parse(source)

    def _is_number(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Constant)
            and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
            and node.value != 0
        )

    def _names_of(target: ast.AST) -> set[str]:
        if isinstance(target, ast.Name):
            return {target.id}
        if isinstance(target, ast.Attribute):
            return {target.attr}
        return set()

    for node in ast.walk(tree):
        # x = 2000  /  x: int = 2000  /  obj.x = 2000
        targets: list[ast.AST] = []
        value: ast.AST | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for t in targets:
            hit = _names_of(t) & TARGET_NAMES
            if hit and value is not None and _is_number(value):
                found.append(f"{sorted(hit)[0]} = {ast.unparse(value)}")

        # f(water_goal_ml=2000)
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg in TARGET_NAMES and _is_number(kw.value):
                    found.append(f"{kw.arg}={ast.unparse(kw.value)}")

        # <что угодно про ориентир> or 2000
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            text = ast.unparse(node)
            if any(name in text for name in TARGET_NAMES):
                if any(_is_number(v) for v in node.values):
                    found.append(f"or-умолчание: {text}")

    return found


#: Имя источника, под условием которого чтение столбца разрешено (§5.1).
PROVENANCE_GATE_NAME = "USER_ENTERED"

#: Вызовы ORM, в которых строка-имя столбца означает ЧТЕНИЕ его значения.
_ORM_VALUE_READERS = frozenset({"values", "values_list", "annotate", "aggregate", "F"})


def _guarded_by_provenance(node: ast.AST, parents: dict[int, ast.AST]) -> bool:
    """Стоит ли узел внутри ``if``, чей предикат называет ``USER_ENTERED``.

    Проверяется ТОЛЬКО ветка ``body`` условия: чтение в ``else`` того же
    ``if`` — это чтение под противоположным условием, и оно нарушение.
    """
    child = node
    parent = parents.get(id(node))
    while parent is not None:
        # Чтение внутри самого предиката (``… and profile.daily_water_ml``)
        # — часть гейта: значение здесь проверяется, а не используется.
        if isinstance(parent, ast.If) and (child in parent.body or child is parent.test):
            if any(
                isinstance(n, ast.Attribute) and n.attr == PROVENANCE_GATE_NAME
                for n in ast.walk(parent.test)
            ):
                return True
        child, parent = parent, parents.get(id(parent))
    return False


def _stale_column_reads(source: str) -> list[str]:
    """Чтения ``daily_water_ml`` — столбца со снятой формулой.

    Считается ЛЮБОЕ чтение значения: атрибут (``profile.daily_water_ml``)
    и строка внутри вызова ORM, отдающего значения (``.values_list(...,
    "daily_water_ml")``). Строки в комментариях и докстрингах не
    считаются — ими как раз объясняют, почему чтения больше нет.

    НЕ считаются (§5.1, 11.09.2026): запись (``profile.daily_water_ml =
    …`` — ctx Store), загрузка столбца ``.only("daily_water_ml")`` без
    чтения значения, и чтение под ``if … USER_ENTERED …`` — единственный
    источник, при котором в столбце число человека, а не формулы.
    """
    found: list[str] = []
    tree = ast.parse(source)
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == STALE_COLUMN:
            if isinstance(node.ctx, ast.Store):
                continue
            if _guarded_by_provenance(node, parents):
                continue
            found.append(f"атрибут {ast.unparse(node)}")
        elif isinstance(node, ast.Call):
            func_name = ast.unparse(node.func)
            # Строка-имя столбца считается чтением только в вызовах ORM,
            # ОТДАЮЩИХ значения. ``.only``/``.defer`` лишь грузят столбец;
            # ``list.append("daily_water_ml")`` — имя в аудите, не число.
            if func_name.rsplit(".", 1)[-1] not in _ORM_VALUE_READERS:
                continue
            for arg in node.args:
                if isinstance(arg, ast.Constant) and arg.value == STALE_COLUMN:
                    found.append(f"ORM-строка в {func_name}(...)")
    return found


def _offenders(check) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for path in _scanned():
        hits = check(path.read_text(encoding="utf-8"))
        if hits:
            out[str(path.relative_to(_ROOT))] = hits
    return out


#: Заведомо виноватый исходник для контроля присутствия. Все формы,
#: которые встречались в этом коде живьём.
_A_SUBSTITUTION_LOOKS_LIKE_THIS = '''
from nutrition.models import NutritionProfile


def summary(profile):
    calories_goal = 2000
    agg = WaterAggregate(water_ml=0, water_goal_ml=2000)
    norm = profile.daily_water_ml or 2000
    rows = NutritionProfile.objects.values_list("daily_water_ml", flat=True)
    return calories_goal, agg, norm, rows
'''

#: Чтения под условием происхождения — разрешённые (§5.1) — и рядом два
#: чтения, которые сторож обязан ловить: под чужим условием и в ``else``.
_A_PROVENANCE_GATED_READ_LOOKS_LIKE_THIS = '''
from nutrition.models import NutritionProfile


def load(profile):
    profile.daily_water_ml = 2200  # запись — не чтение
    if profile.targets_source == NutritionProfile.TargetsSource.USER_ENTERED and profile.daily_water_ml:
        allowed = profile.daily_water_ml
    else:
        forbidden_else = profile.daily_water_ml
    if profile.goal == "lose":
        forbidden_other_if = profile.daily_water_ml
    return allowed, forbidden_else, forbidden_other_if
'''


class TestTargetsStayAbsent:
    def test_nobody_substitutes_a_value_for_absence(self) -> None:
        """Ни одно разбираемое место не кладёт число в ориентир."""
        offenders = _offenders(_substitutions)
        assert offenders == {}, (
            "ориентир снова подставляют вместо отсутствия: "
            + "; ".join(
                f"{f}: {', '.join(h)}" for f, h in sorted(offenders.items())
            )
        )

    def test_nobody_reads_the_removed_formulas_column(self) -> None:
        """Столбец со снятой формулой 30 мл × вес никто не читает.

        Это отдельное утверждение, а не частный случай предыдущего:
        подстановки числа здесь нет вовсе — есть чтение НАСТОЯЩЕГО
        значения из базы. Оно выглядит безупречно («это же его число из
        анкеты») и именно поэтому пережило прошлую чистку.
        """
        offenders = _offenders(_stale_column_reads)
        assert offenders == {}, (
            "снятая формула снова читается из профиля: "
            + "; ".join(
                f"{f}: {', '.join(h)}" for f, h in sorted(offenders.items())
            )
        )

    def test_the_removed_formula_names_are_gone(self) -> None:
        """``WATER_ML_PER_KG`` и ``_water_target`` не вернулись."""
        from nutrition.services import nutrition_profile_service as mod

        alive = sorted(n for n in REMOVED_FORMULA_NAMES if hasattr(mod, n))
        assert alive == [], f"снятая методика вернулась в модуль: {alive}"

    def test_the_scanner_still_sees_a_substitution(self) -> None:
        """Контроль присутствия: сканер не ослеп.

        Три теста выше — утверждения об ОТСУТСТВИИ, и все три прошли бы
        победно в мире, где разбор сломался и не находит уже ничего.
        Поэтому сканер запускается по исходнику, который виноват заведомо,
        и обязан найти в нём все четыре формы возврата.
        """
        subs = _substitutions(_A_SUBSTITUTION_LOOKS_LIKE_THIS)
        assert "calories_goal = 2000" in subs
        assert "water_goal_ml=2000" in subs
        assert any(s.startswith("or-умолчание") for s in subs), subs

        reads = _stale_column_reads(_A_SUBSTITUTION_LOOKS_LIKE_THIS)
        assert any("атрибут" in r for r in reads), reads
        assert any("ORM-строка" in r for r in reads), reads

    def test_the_scanner_tells_a_provenance_gated_read_from_the_rest(self) -> None:
        """§5.1: чтение под ``USER_ENTERED`` разрешено; под другим условием,
        в ``else`` того же ``if`` и запись — нет/да соответственно.

        Стража парная: разрешённое место НЕ в списке (иначе исключение
        не работает), два запрещённых — в списке (иначе исключение
        проглотило всё), записи в списке нет.
        """
        reads = _stale_column_reads(_A_PROVENANCE_GATED_READ_LOOKS_LIKE_THIS)
        assert reads == [
            "атрибут profile.daily_water_ml",  # forbidden_else
            "атрибут profile.daily_water_ml",  # forbidden_other_if
        ], reads
