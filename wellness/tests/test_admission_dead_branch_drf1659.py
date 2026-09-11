"""Мёртвая ветка допуска объявлена мёртвой, и у объявления есть сторож (DRF-1659).

### Что здесь НЕ чинится, и почему

`personal_plan_admission` отказывает по двум причинам, и **вторая недостижима**:

    if ASSUMED_WEIGHT_MARKER in verdict.assumed_inputs:
        return AdmissionDecision(allowed=False, reason_code="assumed_weight")

Маркер снят вместе с подстановкой веса (§103, DRF-1339). Единственный
производитель — `nutrition/services/profile_upsert_service.py` — отдаёт
`"assumed_inputs": []` **всегда**, литералом. Новое имя отказа,
`insufficient_inputs`, живёт в `overrides_applied`, и `wellness/` о нём не
знает: `NutritionVerdict` несёт ровно три поля — `goal`,
`goal_overridden_by`, `assumed_inputs`, — и места для него нет.

Отсюда последствие: профиль **без расчёта** (входов не хватило, ориентиры
NULL) проходит допуск weight-loss как `admitted`, если `goal_overridden_by`
пуст. Гейт не различает «вес настоящий» и «веса нет».

**Чем заменить ветку — решение не принято** (тикет говорит это прямо: читать
`overrides_applied[].reason`, читать `targets_provenance.source`, или снять
ветку и гейтить по наличию ориентира). Выбрать за владельца здесь значило бы
записать политику в тест, а потом предъявить тест как её обоснование.

Поэтому этот модуль **не чинит гейт**. Он делает две вещи, которые определены
без решения:

1. объявляет ветку мёртвой и стережёт объявление — если производитель начнёт
   заполнять `assumed_inputs`, проверка покраснеет и скажет, что ветка ожила;
2. стережёт **момент**, когда дефект станет живым — появление первого
   вызывающего вне тестов.

### Почему второй сторож — главный

Экспозиция сегодня ноль: `personal_plan_admission` не зовёт никто, кроме
тестов. Тикет заведён ровно затем, «чтобы дефект не потерялся до того, как у
допуска появится вызывающий». Это надежда на память.

Сторож превращает её в событие: тот, кто подключит допуск, **не сможет этого
сделать молча** — проверка упадёт и назовёт, что именно гейт не различает. Это
единственная точка, где предупреждение приходит вовремя: не раньше (сегодня
чинить нечего) и не позже (после подключения дефект уже в проде).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCER = REPO_ROOT / "nutrition" / "services" / "profile_upsert_service.py"
ADMISSION_FUNC = "personal_plan_admission"


# ---------------------------------------------------------------------------
# 1. Ветка мертва, потому что производитель отдаёт пустой список литералом
# ---------------------------------------------------------------------------


def _assumed_inputs_values(source: str) -> list[ast.AST]:
    """Все значения, которые исходник кладёт под ключ `assumed_inputs`."""

    found: list[ast.AST] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if isinstance(key, ast.Constant) and key.value == "assumed_inputs":
                    found.append(value)
    return found


def _is_empty_list_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.List) and not node.elts


def test_the_only_producer_still_emits_an_empty_list() -> None:
    """Пока это пустой литерал, ветка `assumed_weight` недостижима.

    Проверяется **литерал**, а не поведение: значение, собранное в рантайме,
    пришлось бы звать с базой, и тогда сторож молчал бы ровно в том случае,
    который важен, — когда кто-то заменит литерал на вычисление.
    """
    values = _assumed_inputs_values(PRODUCER.read_text(encoding="utf-8"))

    # Сначала — что ключ вообще найден. Ноль совпадений прошёл бы проверку
    # ниже молча и означал бы «не измеряли», а не «пусто».
    assert values, f"ключ assumed_inputs не найден в {PRODUCER.name} — измерялся не тот предмет"
    assert all(_is_empty_list_literal(v) for v in values), (
        f"{PRODUCER.name} снова заполняет assumed_inputs. Значит ветка "
        "`assumed_weight` в wellness/admission.py ожила — а её докстринг "
        "объявляет мёртвой. Смотреть DRF-1659 до того, как полагаться на гейт."
    )


def test_the_matcher_would_notice_a_non_empty_list() -> None:
    """Положительный контроль к предыдущему.

    Без него тест зеленел бы и на матчере, который не находит ничего никогда, —
    то есть «пусто» было бы неотличимо от «не искали».
    """
    values = _assumed_inputs_values('payload = {"assumed_inputs": ["weight_kg"]}')

    assert len(values) == 1
    assert not _is_empty_list_literal(values[0])


# ---------------------------------------------------------------------------
# 2. Первый вызывающий обязан узнать о дефекте в момент подключения
# ---------------------------------------------------------------------------


def _calls_to(name: str, source: str) -> int:
    """Сколько раз в исходнике ВЫЗЫВАЕТСЯ функция с этим именем.

    Именно вызовы: определение и импорт не считаются, иначе сторож краснел бы
    на собственном модуле.
    """
    calls = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == name:
            calls += 1
        elif isinstance(func, ast.Attribute) and func.attr == name:
            calls += 1
    return calls


def _production_callers() -> list[str]:
    hits: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        posix = path.as_posix()
        if "/tests/" in posix or path.name.startswith("test_"):
            continue
        if "/.venv/" in posix or "/migrations/" in posix:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if ADMISSION_FUNC not in source:
            continue
        try:
            count = _calls_to(ADMISSION_FUNC, source)
        except SyntaxError:
            continue
        if count:
            hits.append(f"{path.relative_to(REPO_ROOT).as_posix()} ({count})")
    return hits


def test_nobody_wires_the_gate_up_while_it_cannot_tell_the_two_apart() -> None:
    """Ноль вызывающих — не «всё хорошо», а «дефект ещё не проявился».

    Когда этот тест покраснеет, он покраснеет **правильно**: у допуска
    появился вызывающий, а гейт по-прежнему пропускает профиль без расчёта.
    Красный здесь — не запрет подключать, а требование сначала закрыть
    DRF-1659: решить, что читает допуск вместо мёртвой ветки.
    """
    callers = _production_callers()

    assert callers == [], (
        "у personal_plan_admission появился вызывающий вне тестов: "
        f"{callers}. Гейт сейчас НЕ различает «вес настоящий» и «веса нет»: "
        "ветка assumed_weight мертва с §103, а insufficient_inputs допуск не "
        "читает. Профиль без расчёта пройдёт как admitted. Закрыть DRF-1659 "
        "перед подключением."
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("personal_plan_admission(v, outcome_target=t, direction=d)", 1),
        ("wellness.admission.personal_plan_admission(v)", 1),
        ("def personal_plan_admission(verdict): ...", 0),
        ("from wellness.admission import personal_plan_admission", 0),
        ("x = personal_plan_admission", 0),
    ],
)
def test_the_caller_scanner_counts_calls_and_not_mentions(source: str, expected: int) -> None:
    """Положительный контроль к сторожу выше, в обе стороны.

    Он обязан находить настоящий вызов — иначе «ноль вызывающих» означает «не
    умеет искать». И обязан **не** считать определение, импорт и ссылку на имя:
    иначе покраснеет на самом `admission.py` и будет отключён как шумный
    задолго до того, как понадобится.
    """
    assert _calls_to(ADMISSION_FUNC, source) == expected
