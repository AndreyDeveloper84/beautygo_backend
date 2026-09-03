"""Распознавание НАЗВАННОЙ услуги в свободном тексте цели (DRF-1451).

Зачем это существует
--------------------

Владелец 03.09.2026: рядом с вопросами анкеты должен стоять запрос уже
известной услуги. Человек со словами «хочу маникюр» называет её прямо на
поверхности анкеты и идёт к подбору, **не отвечая ни на один вопрос**
(поправка A-1 к BOT-001, §24, условие C-2).

Механики под это заводить не надо: свободный ввод (`goal_text`) уже
существует. Не хватало одного — чтобы названная услуга **признавалась
готовой целью**. Без этого `build_decision_context` видел цель без
`goal_key` и выдавал `goal_clarification`, то есть ронял человека обратно
в вопросы — ровно то, что решение владельца запрещает.

Почему это НЕ нечёткий маппинг (OD-1 не нарушен)
-------------------------------------------------

OD-1 / Ответ 3 запрещают отображать свободный текст «в ближайший чип» и
сознательно не вводят порог уверенности: калибровать его не на чем
(OD-2 — корпуса формулировок нет).

Здесь порога нет и близости нет. Есть ровно одна операция: **известное
имя целиком присутствует в тексте как последовательность слов**.
«хочу маникюр» содержит «маникюр» дословно; «хочу что-то для рук» не
содержит ничего и остаётся неразрешённым — уточнение, как и раньше.
Ни расстояния Левенштейна, ни эмбеддингов, ни «ближайшего», ни порога:
детерминированная функция, у которой на одинаковом входе и одинаковом
каталоге всегда один ответ.

Из нескольких совпадений берётся **самое длинное** (самое конкретное):
«маникюр с покрытием» выигрывает у «маникюр». Ничья по длине разводится
по имени — чтобы ответ не зависел от порядка строк в БД.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from services.models import SalonService, ServiceCategory

# Всё, что не буква и не цифра, — разделитель. Дефисы и точки внутри
# названий («аппаратный маникюр», «SPA-уход») распадаются на слова
# одинаково с обеих сторон сравнения, поэтому совпадение не теряется.
_WORD_RE = re.compile(r"[^\w]+", flags=re.UNICODE)

# Потолок длины текста, в котором мы вообще ищем имя услуги. Короткая
# фраза («хочу маникюр») — это название услуги с обрамлением. Длинный
# рассказ — это цель своими словами, и выдёргивать из него совпавшее
# слово значило бы угадывать, чего человек хочет.
MAX_TEXT_WORDS = 12


@dataclass(frozen=True)
class NamedService:
    """Найденное в тексте имя из каталога."""

    kind: str  # "category" | "service"
    name: str


def _words(value: str) -> list[str]:
    return [w for w in _WORD_RE.split(value.casefold()) if w]


def _contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    """Встречается ли ``needle`` в ``haystack`` как непрерывная цепочка слов."""
    if not needle or len(needle) > len(haystack):
        return False
    last = len(haystack) - len(needle)
    size = len(needle)
    return any(haystack[i:i + size] == needle for i in range(last + 1))


def _catalog_names() -> list[NamedService]:
    """Активные имена каталога — категории и услуги салона.

    Тенант не фильтруется — так же, как в ``_suggestions()`` и
    ``resolve_goal_category_ids``: слой цели работает от клиента, а не от
    салона, и вводить здесь тенантную границу в одиночку означало бы
    разойтись с соседями по модулю.
    """
    names = [
        NamedService(kind="category", name=name)
        for name in ServiceCategory.objects.filter(is_active=True).values_list(
            "name", flat=True
        )
    ]
    names += [
        NamedService(kind="service", name=name)
        for name in SalonService.objects.filter(is_active=True).values_list(
            "name", flat=True
        )
    ]
    return names


def match_named_service(text: str | None) -> NamedService | None:
    """Названа ли в ``text`` услуга или категория из каталога.

    ``None`` — не названа. Тогда цель остаётся неразрешённой и документ,
    как и раньше, просит уточнить (``goal_clarification``).
    """
    if not text:
        return None

    haystack = _words(text)
    if not haystack or len(haystack) > MAX_TEXT_WORDS:
        # Длинный рассказ — это не «названная услуга», а формулировка
        # цели своими словами. Разбирать её здесь значило бы угадывать.
        return None

    best: NamedService | None = None
    best_len = 0
    for candidate in _catalog_names():
        needle = _words(candidate.name)
        if not _contains_sequence(haystack, needle):
            continue
        length = len(needle)
        if length > best_len or (
            length == best_len and best is not None and candidate.name < best.name
        ):
            best, best_len = candidate, length
    return best
