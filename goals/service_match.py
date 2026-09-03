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

from django.core.cache import cache

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


#: Ключ и срок кеша списка имён. Каталог меняется редко, а читается
#: теперь на каждом открытии приложения — см. ниже.
_CACHE_KEY = "goals.service_match.catalog_names.v1"
_CACHE_TTL_SECONDS = 300

#: Потолок числа имён. Не оптимизация, а предохранитель: без него
#: зеркало YClients на тысяче салонов превращает каждую проверку в
#: последовательное чтение всей таблицы услуг.
MAX_CATALOG_NAMES = 5000


def _load_catalog_names() -> list[tuple[str, str]]:
    names = [
        ("category", name)
        for name in ServiceCategory.objects.filter(is_active=True)
        .order_by("name")
        .values_list("name", flat=True)[:MAX_CATALOG_NAMES]
    ]
    remaining = MAX_CATALOG_NAMES - len(names)
    if remaining > 0:
        names += [
            ("service", name)
            for name in SalonService.objects.filter(is_active=True)
            .order_by("name")
            .values_list("name", flat=True)[:remaining]
        ]
    return names


def _catalog_names() -> list[NamedService]:
    """Активные имена каталога — категории и услуги салона.

    # Кеш

    Функция вызывается из ``build_decision_context``, то есть на каждом
    открытии приложения и на каждом ответе сервера, для любого клиента
    с текстовой целью. Два запроса без ``LIMIT`` на такой частоте —
    ошибка: у ``SalonService`` индексы начинаются с ``tenant``, поэтому
    голый фильтр по ``is_active`` читает таблицу последовательно.
    Пятиминутный кеш и потолок ``MAX_CATALOG_NAMES`` держат стоимость
    ограниченной; каталог меняется несравнимо реже.

    # Тенант НЕ фильтруется, и это известное ограничение

    Соседи по модулю (``_suggestions``, ``resolve_goal_category_ids``)
    тоже не фильтруют — но они читают ``GoalOption``, действительно
    глобальную курируемую таблицу. Каталог глобальным не является, и
    прецедент сюда не переносится: клиент салона A может получить
    «цель распознана» от имени, которое есть только у салона B.

    Наружу утекает один булев признак, не данные. Но решение при этом
    принимается по чужим строкам, и чинить это надо тенантом,
    протянутым до слоя цели, — чего в ``ClientGoal`` сегодня нет.
    Записано вопросом владельцу, а не замазано здесь.
    """
    cached = cache.get(_CACHE_KEY)
    if cached is None:
        cached = _load_catalog_names()
        cache.set(_CACHE_KEY, cached, _CACHE_TTL_SECONDS)
    return [NamedService(kind=kind, name=name) for kind, name in cached]


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
