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

Замер 07.09.2026: вернуть подстановку в ``water_service`` →
``2 failed``; снять → зелено.
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

#: Места, где подстановка ещё осталась — названы, а не забыты.
#:
#: Оба чинятся отдельной работой: они вне пакета этой правки, и трогать
#: их здесь значило бы расширить диф настолько, что ревью перестанет быть
#: возможным. Форма у обоих одна и та же — «норма человека, а если её нет,
#: то общая»:
#:
#: * ``pattern_detection_service._profile_goal_{protein,water,kcal}`` —
#:   движок паттернов;
#: * ``returning_success_service._resolve_goal`` — калории для сводки
#:   вернувшегося.
#:
#: Третье место живёт вне этого каталога и потому сюда не попадает:
#: ``notifications/tasks.py`` (напоминание про воду, строки 279 и 344).
#:
#: Список ждёт, что его сократят: когда место починят, второй тест ниже
#: упадёт и заставит убрать строку, а не оставить её навсегда.
KNOWN_REMAINING = frozenset({
    "pattern_detection_service.py",
    "returning_success_service.py",
})

_SERVICES = Path(__file__).resolve().parents[1] / "services"


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
    for path in sorted(_SERVICES.glob("*.py")):
        names = _reads_forbidden_settings(path.read_text(encoding="utf-8"))
        if names:
            out[path.name] = names
    return out


class TestNormsAreNotSubstituted:
    def test_no_service_invents_a_norm(self) -> None:
        """Ни один сервис питания не подставляет чужое число вместо нормы."""
        offenders = _offenders()

        assert set(offenders) <= KNOWN_REMAINING, (
            "подстановка нормы вернулась: "
            + "; ".join(f"{f} читает {sorted(n)}" for f, n in sorted(offenders.items()))
        )

    def test_the_remaining_site_is_still_there(self) -> None:
        """Контроль присутствия: список остатка не протух.

        Без него страж выше прошёл бы победно и в мире, где сканер
        перестал находить что угодно — например, если переименуют
        настройки или сломается разбор. Здесь же чинится и обратное: когда
        ``pattern_detection_service`` вылечат, этот тест упадёт и заставит
        убрать строку из ``KNOWN_REMAINING``, а не оставить её навсегда.
        """
        assert set(_offenders()) == KNOWN_REMAINING
