"""Действует ли ориентир — один предикат на всех читателей (§5.1, §6).

Решение владельца 11.09.2026 §5.1: «результат становится привычкой/шагом
только после подтверждения пользователя». Значит у ориентира есть
состояние «посчитан, но не подтверждён» — ``ayla_proposed`` — и в нём
число показывается человеку как предложение, но НЕ участвует ни в
«осталось на сегодня», ни в оценках «мало/много», ни в паттернах и
инсайтах: для них предложение равно отсутствию (§6: неизвестные нормы —
``NOT_CONFIGURED``, а не ноль; и не «почти норма»).

Множество действующих источников перечислено ЯВНО и совпадает с тем,
что читает бот (``targets_are_configured`` ⇔ источник ∈ {``ayla_calculated``,
``user_entered``}). Всё, что не названо, — не действует: новое состояние
источника попадёт сюда только руками, а не по умолчанию.

Читатели значений (``daily_kcal``, ``daily_protein_g``, RDA) обязаны
спрашивать этот предикат, а не истинность числа: у ``ayla_proposed``
число есть, и по нему одному предложение неотличимо от подтверждённого.
"""
from __future__ import annotations

from nutrition.models import NutritionProfile

#: Источники, при которых ориентир ДЕЙСТВУЕТ. Расширять только решением
#: владельца и синхронно с ботом (``TARGETS_CONFIGURED_SOURCES``).
CONFIRMED_SOURCES: frozenset[str] = frozenset({
    NutritionProfile.TargetsSource.AYLA_CALCULATED,
    NutritionProfile.TargetsSource.USER_ENTERED,
})


#: Виды ориентира, у каждого своё происхождение (DRF-1929, F1(б)).
#: Имена совпадают с ключами ``targets_method_versions`` — там грань по
#: видам проведена раньше и по той же причине (§85).
KIND_CALORIES = "calories"
KIND_FLUIDS = "fluids"

#: Вид → поле происхождения. Словарь, а не ветвление: новый вид
#: добавляется сюда, и все читатели получают его сразу.
KIND_SOURCE_FIELD: dict[str, str] = {
    KIND_CALORIES: "calories_source",
    KIND_FLUIDS: "fluids_source",
}


#: Поля ориентира по виду. Калории — всё, что выведено из расчёта энергии:
#: макросы, ``bmr`` и RDA; жидкость — вода (DRF-1929, DRF-2192).
KIND_FIELDS: dict[str, tuple[str, ...]] = {
    KIND_CALORIES: (
        "bmr", "daily_kcal", "daily_protein_g", "daily_fat_g", "daily_carbs_g",
        "daily_vitamin_d_iu", "daily_vitamin_b12_mcg", "daily_vitamin_c_mg",
        "daily_iron_mg", "daily_calcium_mg", "daily_magnesium_mg",
        "daily_omega3_g", "daily_fiber_g",
    ),
    KIND_FLUIDS: ("daily_water_ml",),
}

#: Вид → поле подтверждения (пара к ``KIND_SOURCE_FIELD``).
KIND_STAMP_FIELD: dict[str, str] = {
    KIND_CALORIES: "calories_confirmed_at",
    KIND_FLUIDS: "fluids_confirmed_at",
}


def kind_source(profile: NutritionProfile | None, kind: str) -> str | None:
    """Происхождение ОДНОГО вида — или ``None``, если по видам не писалось.

    ``None`` возвращается у строк, которые ещё не писали по-видовое
    происхождение. Существующим строкам имя дала миграция ``0023``
    (решение владельца 16.09.2026), поэтому после неё ``NULL`` — редкий
    случай, а не общее состояние. Пока он встречается, читатели падают
    обратно на общее ``targets_source`` — ЯВНО и временно: иначе строка
    без по-видовой подписи молча потеряла бы действующий ориентир.
    """
    if profile is None:
        return None
    field = KIND_SOURCE_FIELD.get(kind)
    if field is None:
        raise ValueError(f"Неизвестный вид ориентира: {kind!r}")
    return getattr(profile, field, None)


def kind_confirmed(profile: NutritionProfile | None, kind: str) -> bool:
    """Действует ли ориентир ЭТОГО вида — по происхождению, не по числу."""
    if profile is None:
        return False
    source = kind_source(profile, kind)
    if source is None:
        # Названный временный откат: строка до DRF-1929. Не «считаем
        # подтверждённым», а «спрашиваем прежнюю общую подпись», чтобы
        # существующие клиенты не потеряли ориентир до решения владельца.
        source = profile.targets_source
    return source in CONFIRMED_SOURCES


def calories_confirmed(profile: NutritionProfile | None) -> bool:
    """Действует ли ориентир по калориям (и всё, что из него выведено)."""
    return kind_confirmed(profile, KIND_CALORIES)


def fluids_confirmed(profile: NutritionProfile | None) -> bool:
    """Действует ли ориентир по жидкости."""
    return kind_confirmed(profile, KIND_FLUIDS)


def targets_confirmed(profile: NutritionProfile | None) -> bool:
    """Действует ли ориентир профиля — по происхождению, не по числу.

    Оставлен для читателей, которым нужен НАБОР целиком. После F1(б)
    таких в каталоге нет: каждый читатель знает свой вид и обязан
    спрашивать ``calories_confirmed`` / ``fluids_confirmed``. Смысл здесь
    — «действует хоть один вид»: ослабить прежнее поведение он не может.
    """
    if profile is None:
        return False
    return calories_confirmed(profile) or fluids_confirmed(profile)


def effective_kind_source(profile: NutritionProfile, kind: str) -> str:
    """Подпись вида с тем же откатом на общую, что у ``kind_confirmed``."""
    return kind_source(profile, kind) or profile.targets_source


#: Порядок, в котором общая подпись выводится из подписей видов. Первым —
#: предложение: пока хоть один вид — неподтверждённое предложение, читатель
#: общей подписи (бот до перехода на ``by_kind``) обязан видеть «не
#: подтверждено», а не «действует» (§6). Так же вела себя строка до
#: DRF-2192, когда пересчёт переводил в предложение все виды разом.
_OVERALL_PRIORITY: tuple[str, ...] = (
    NutritionProfile.TargetsSource.AYLA_PROPOSED,
    NutritionProfile.TargetsSource.USER_ENTERED,
    NutritionProfile.TargetsSource.AYLA_CALCULATED,
    NutritionProfile.TargetsSource.UNKNOWN_LEGACY,
)


def overall_source(profile: NutritionProfile) -> str:
    """Общая подпись, выведенная из подписей видов (DRF-2192)."""
    sources = {effective_kind_source(profile, kind) for kind in KIND_FIELDS}
    for source in _OVERALL_PRIORITY:
        if source in sources:
            return source
    return NutritionProfile.TargetsSource.NONE
