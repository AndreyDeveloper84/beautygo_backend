"""Норму нельзя подставлять из настроек — страж на возврат дефекта.

Восьмёрка стаканов возвращалась ДВАЖДЫ. Сначала её выкинули из клиента
(`apps/miniapp_api/views.py` в ai-bot-platform, со словами «норму воды не
придумываем, восемь — число ниоткуда»), а она вернулась по проводу:
``NUTRITION_DEFAULT_WATER_GOAL_ML = 2000`` при стакане 250 мл — ровно те
же восемь стаканов, только подставленные на нашей стороне. Поведенческие
стражи ловят это по одному месту за раз; здесь — по всем сразу.

Три настройки, о которых речь, — это ЧУЖИЕ числа, поданные как свои:

* ``NUTRITION_DEFAULT_WATER_GOAL_ML`` — дневная норма воды;
* ``NUTRITION_DEFAULT_CALORIES_GOAL`` — дневная цель по калориям;
* ``NUTRITION_DEFAULT_PROTEIN_GOAL_G`` — знаменатель недельного дефицита
  белка, который уходит в промпт модели как факт о человеке.

Витаминные ``NUTRITION_DEFAULT_*`` сюда НЕ входят и входить не должны:
RDA — популяционная норма по определению, она не выводится из веса и не
притворяется персональной.

Проверка разбором исходника, а не поведением: «этот модуль НЕ читает
настройку» — утверждение об отсутствии обращения, и поведением оно
неотличимо от «читает, но сегодня значение совпало».

Правка 08.09.2026 — список остатка сократился до пустого, и страж
поменял форму
-----------------------------------------------------------------

Страж заводился с признанием: три места он назвал, но починить не мог,
и держал их в ``KNOWN_REMAINING``, чтобы список сократили, а не забыли.
Все три починены (``pattern_detection_service``,
``returning_success_service``, ``notifications/tasks.py``), поэтому:

1. ``KNOWN_REMAINING`` пуст — исключений больше нет ни одного, и первый
   тест теперь требует ровно то, чем назван: НИ ОДНОГО чтения.
2. Область разбора выросла. Раньше сканировался только
   ``nutrition/services/*.py``, и третье место — напоминание про воду в
   ``notifications/tasks.py`` — было названо словами в комментарии,
   потому что живёт вне каталога. Слова не краснеют; теперь этот файл
   разбирается наравне с сервисами, и подстановка, вернувшаяся в самое
   острое из трёх мест (исходящий пуш), роняет страж, а не остаётся
   упоминанием.
3. Контроль присутствия сменил предмет. Он существует затем, чтобы
   страж не прошёл победно в мире, где сканер ослеп, — и раньше опирался
   на живого нарушителя: «в списке ровно ``pattern_detection_service``».
   Нарушителей не осталось, такая проверка стала бы ``set() == set()`` и
   прошла бы при любой поломке разбора. Вместо неё сканер запускается по
   заведомо виноватому исходнику, собранному прямо в тесте, — обе формы
   обращения, ``settings.NAME`` и ``getattr``.

Чего не ловит ни один из двух тестов: переименование самих настроек.
Переименуют ``NUTRITION_DEFAULT_WATER_GOAL_ML`` вместе с местом чтения —
``FORBIDDEN`` протухнет молча. Лечится только тем, что новое имя добавят
сюда руками.

Замер 07.09.2026: вернуть подстановку в ``water_service`` →
``2 failed``; снять → зелено.
Замер 08.09.2026: вернуть подстановку в ``notifications/tasks.py`` →
``1 failed``; снять → ``2 passed``.
"""
from __future__ import annotations

import ast
from pathlib import Path

#: Настройки, которые нельзя читать как «норму этого человека».
FORBIDDEN = frozenset({
    "NUTRITION_DEFAULT_WATER_GOAL_ML",
    "NUTRITION_DEFAULT_CALORIES_GOAL",
    "NUTRITION_DEFAULT_PROTEIN_GOAL_G",
})

#: Места, где подстановка ещё осталась. ПУСТО — и должно остаться пустым.
#:
#: Здесь стояли ``pattern_detection_service.py`` и
#: ``returning_success_service.py``: страж заводился вместе с починкой
#: сводки и экрана, назвал оставшееся поимённо и ждал, что список
#: сократят. Оба места починены — ``_profile_goal_{protein,water,kcal}``
#: и ``_resolve_goal`` возвращают ноль вместо общего числа, — вместе с
#: третьим, ``notifications/tasks.py``, которое теперь тоже разбирается
#: (см. ``_SCANNED`` ниже).
#:
#: Добавлять сюда новую строку — значит согласиться, что чужое число
#: снова выдают человеку за его норму. Правильный ход — починить место.
KNOWN_REMAINING: frozenset[str] = frozenset()

_ROOT = Path(__file__).resolve().parents[2]

#: Что разбирается. Сервисы питания — и напоминание про воду, живущее
#: вне пакета: пуш уезжает человеку сам, без запроса, поэтому оставлять
#: его под честное слово комментария нельзя.
_SCANNED: tuple[Path, ...] = (
    *sorted((_ROOT / "nutrition" / "services").glob("*.py")),
    _ROOT / "notifications" / "tasks.py",
)


def _reads_forbidden_settings(source: str) -> set[str]:
    """Имена запрещённых настроек, которые модуль читает.

    Ловит обе формы: ``settings.NAME`` и ``getattr(settings, "NAME", ...)``.
    Строки в комментариях и докстрингах не считаются — они и есть то, чем
    объясняют, почему подстановки больше нет.
    """
    found: set[str] = set()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN:
            found.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in FORBIDDEN
        ):
            found.add(node.args[1].value)
    return found


def _offenders() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for path in _SCANNED:
        names = _reads_forbidden_settings(path.read_text(encoding="utf-8"))
        if names:
            out[path.name] = names
    return out


#: Заведомо виноватый исходник для контроля присутствия ниже. Обе формы
#: обращения — та, что стояла в ``water_service`` (``settings.NAME``), и
#: та, что стояла в ``pattern_detection_service`` (``getattr``).
_A_SUBSTITUTION_LOOKS_LIKE_THIS = """
from django.conf import settings

def goal(profile):
    if profile and profile.daily_water_ml:
        return profile.daily_water_ml
    return settings.NUTRITION_DEFAULT_WATER_GOAL_ML

def kcal(profile):
    return getattr(settings, "NUTRITION_DEFAULT_CALORIES_GOAL", 2000)
"""


class TestNormsAreNotSubstituted:
    def test_nobody_invents_a_norm(self) -> None:
        """Ни одно из разбираемых мест не подставляет чужое число.

        Исключений не осталось: ``KNOWN_REMAINING`` пуст, и любое чтение
        трёх настроек — возврат дефекта, а не признанный остаток.
        """
        offenders = _offenders()

        assert set(offenders) <= KNOWN_REMAINING, (
            "подстановка нормы вернулась: "
            + "; ".join(f"{f} читает {sorted(n)}" for f, n in sorted(offenders.items()))
        )

    def test_the_scanner_still_sees_a_substitution(self) -> None:
        """Контроль присутствия: сканер не ослеп.

        Тест выше — утверждение об ОТСУТСТВИИ, и оно проходит победно в
        мире, где разбор сломался и не находит уже ничего. Раньше от
        этого страховал живой нарушитель из ``KNOWN_REMAINING``;
        нарушителей не осталось, и такая страховка выродилась бы в
        ``set() == set()``. Поэтому сканер запускается по исходнику,
        который виноват заведомо.
        """
        assert _reads_forbidden_settings(_A_SUBSTITUTION_LOOKS_LIKE_THIS) == {
            "NUTRITION_DEFAULT_WATER_GOAL_ML",
            "NUTRITION_DEFAULT_CALORIES_GOAL",
        }

    def test_the_water_reminder_is_scanned(self) -> None:
        """Напоминание про воду разбирается, а не только упоминается.

        Третье место жило вне ``nutrition/services`` и потому попадало в
        стража только словами комментария. Слова не краснеют — а это
        единственное из трёх мест, где выдуманная норма уезжала человеку
        сама, без запроса.
        """
        assert (_ROOT / "notifications" / "tasks.py") in _SCANNED
