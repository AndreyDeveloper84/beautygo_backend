"""NutritionProfile compute + override ladder (DRF-300).

Spec: docs/plans/maxbot-phase3-ayla-spec.md §1.

Pure-math: BMR (Mifflin-St Jeor), daily targets (kcal/protein/fat/carbs/
water), and the override ladder that coerces unsafe goals into safe
ones. No LLM, no external API, deterministic — every input maps to one
output and the override audit is fully reproducible.

Health-факторы — ОТКАЗ, не поправка (§5.1, решение владельца 11.09.2026:
«При health-факторах Ayla не рассчитывает индивидуальную норму»):
  ``pregnant`` / ``breastfeeding`` / ``eating_disorder`` / несовершеннолетний
  возраст (§85, раздел 7) → ориентиров нет, в аудите ``health_factor_<имя>``
  на КАЖДЫЙ фактор. Прежняя лестница «беременность → maintain + 200/400 ккал»,
  «РПП → maintain» снята: она считала число там, где считать нельзя.

Override priority (highest first) — что осталось:
  1. BMR floor ladder — if computed daily_kcal < BMR + 100:
     - first try pace=gentle (smaller deficit)
     - if still under floor, fall back to goal=maintain

Defaults (per spec §1.2): if a field is in ``_skipped_fields``, fill
with the median of the Penza pilot audience — ``female / 40 / 165 / 70``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------


# Медианы пензенской аудитории УДАЛЕНЫ (§82, §85; §35 п.10).
#
# Стояло::
#
#     DEFAULT_GENDER = "female"
#     DEFAULT_AGE = 40
#     DEFAULT_HEIGHT_CM = 165
#     DEFAULT_WEIGHT_KG = 70.0   # «Penza pilot audience median»
#
# и подставлялось за ЛЮБОЕ пропущенное поле. Человек, не назвавший вес,
# получал ориентир, посчитанный ОТ ЧУЖОГО ТЕЛА, и на экране это было
# неотличимо от своего.
#
# Это тяжелее плоской константы, а не легче. Плоскую 2000 видно — она
# одинаковая у всех, и рано или поздно кто-то замечает. Медиана даёт
# правдоподобное и РАЗНОЕ число: оно меняется от ответов человека и
# потому выглядит персональным. Сверить его не с чем, оспорить нечем.
#
# DRF-1339 завёл маркер ``{"reason": "assumed_input", "field":
# "weight_kg"}`` — но маркер это признание, а не отказ: число всё равно
# считалось, уезжало в профиль и показывалось. Теперь пропуск любого из
# четырёх обязательных входов отменяет расчёт целиком, и у отказа есть
# имя: ``insufficient_inputs`` с перечнем недостающих полей.
#
# Раздел 3.2 решения перечисляет входы как ОБЯЗАТЕЛЬНЫЕ: возраст, рост,
# вес, физиологический пол. Раздел 11: «для каждого результата
# воспроизводимы входы, формула и версия» — подставленный вход
# воспроизводимость ломает молча.

#: Входы, без которых расчёта нет. Пол, возраст, рост и вес — ровно те
#: четыре, что стоят в формуле Миффлина — Сан Жеора.
REQUIRED_INPUTS: tuple[str, ...] = ("gender", "age", "height_cm", "weight_kg")

#: Версия методики расчёта калорий — §85, решение владельца 09.09.2026:
#: Миффлин — Сан Жеор с коэффициентом активности и поправкой на цель.
#:
#: Версия здесь не украшение, а условие воспроизводимости. §85 требует
#: двух вещей сразу: те же входы и та же версия дают тот же результат, и
#: изменение методики НЕ переписывает молча уже показанные значения.
#: Строка, сохранённая рядом с ориентиром, — единственное, что позволит
#: через полгода сказать, по какой формуле посчитано число, которое
#: человек видит на экране.
CALORIES_METHOD_VERSION: str = "mifflin_st_jeor_v1"

#: Входы, уходящие в снимок вместе с результатом. Список ШИРЕ, чем
#: ``REQUIRED_INPUTS``: активность, цель и темп на результат влияют, и без
#: них расчёт не воспроизвести.
#:
#: ``health_flags`` в снимок НЕ входят намеренно. Это спецкатегория
#: 152-ФЗ, и держать её второй копией рядом с ориентиром значило бы
#: расширить периметр хранения ради воспроизводимости, которой она не
#: добавляет: след лестницы переопределений и так пишется в
#: ``overrides_applied``, причём именами причин, а не самими признаками.
SNAPSHOT_INPUTS: tuple[str, ...] = (
    "gender",
    "age",
    "height_cm",
    "weight_kg",
    "activity_coefficient",
    "goal",
    "pace",
)

# ``DEFAULT_ACTIVITY`` оставлен и НЕ снят здесь намеренно. Он того же
# класса — умолчание, равное осмысленному значению, — но живёт ещё и в
# схеме: ``NutritionProfile.activity_coefficient = FloatField(default=1.4)``.
# Снять его значит тронуть колонку, то есть миграцию существующих
# клиентов, а это отдельный срез. Названо главному окну строкой.
DEFAULT_ACTIVITY = 1.4

# BMR floor margin — daily_kcal must stay at least this far above BMR
# before we accept a deficit. Anything tighter would mean eating less
# than the body's resting expenditure, which is the canonical
# under-eating boundary in nutrition guidelines.
BMR_FLOOR_MARGIN_KCAL = 100

# Goal multipliers applied to TDEE = BMR × activity_coefficient.
GOAL_FACTORS = {
    "lose": 0.80,        # ~20% deficit
    "tone": 0.90,        # mild deficit + protein priority
    "maintain": 1.00,
    "gain": 1.10,        # ~10% surplus
}

PACE_FACTORS = {
    "gentle": 0.92,      # softer effective deficit/surplus
    "moderate": 1.00,
}

# Health-факторы (§5.1, 11.09.2026): при любом из них расчёт ОТКАЗЫВАЕТ
# с именем — норма не считается, предложение не создаётся. Состав —
# решение главного окна 11.09 по §5.1/§85: три флага профиля плюс
# несовершеннолетие. «Заболевания» свободным текстом и темп > 0.9 кг/нед
# сюда не входят: первое — не флаг профиля, второе — ограничение
# параметра расчёта, а не health-фактор.
#
# Прежние поправки PREGNANCY_KCAL_BONUS / BREASTFEEDING_KCAL_BONUS /
# PREGNANCY_PROTEIN_BONUS_G сняты вместе с лестницей: они были числом,
# посчитанным там, где владелец запретил считать.
HEALTH_FACTOR_FLAGS: tuple[str, ...] = ("pregnant", "breastfeeding", "eating_disorder")
#: Порог совершеннолетия для расчёта (§85 раздел 7: несовершеннолетние —
#: стоп-сценарий). Анкета принимает возраст с 16 (сериализатор), расчёт —
#: с 18: приём данных и расчёт по ним — разные гейты.
ADULT_AGE = 18
HEALTH_FACTOR_MINOR = "minor"

# Ориентир по жидкости — СПРАВОЧНЫЙ ПО ПОЛУ, не формула от веса.
#
# История, которую стоит помнить целиком. Стояло ``WATER_ML_PER_KG = 30``
# и ``_water_target(weight_kg, flags)`` = 30 × вес, плюс 300 при
# беременности и 700 при кормлении. Владелец 09.09.2026 (§82, §85;
# ``docs/decisions/AYLA_NUTRITION_TARGETS_ARCHITECTURE_DECISION.md``
# раздел 4) снял формулу целиком: «Формула воды 30 мл × вес и прибавки
# за беременность или кормление не используются без отдельно
# утверждённой методики». Непустая норма от веса называла человеку его
# вес числом на экране (§35 п.10). Замена — раздел 4 того же решения,
# дословно: «Для здорового совершеннолетнего клиента Ayla предлагает
# справочный ориентир по напиткам: женщина 2200 мл/сутки, мужчина
# 3000 мл/сутки. Это ориентир только по выпитой жидкости… Активность,
# жара, беременность, кормление и заболевания автоматически к значению
# не прибавляются».
#
# «Здоровый совершеннолетний» здесь не подразумевается, а проверен: до
# этой точки расчёт доходит только после ``_health_factors`` (N-g), то
# есть без беременности, кормления, РПП и до 18 лет расчёта нет вовсе —
# и ориентира по жидкости вместе с ним. Согласие на пол — то же
# утверждение ``personal_calculation``, под которым идёт весь расчёт.
#
# Число не зависит ни от веса, ни от активности — поэтому оно не
# называет о человеке ничего, кроме пола, который он сам назвал.
FLUIDS_METHOD_VERSION: str = "adult_beverages_reference_v1"
FLUIDS_REFERENCE_ML: dict[str, int] = {"female": 2200, "male": 3000}


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass
class ProfileInputs:
    """All upsert-time inputs needed by ``compute_norms``.

    Mirrors the fields the bot's POST body sets — we never read
    NutritionProfile directly here so the function stays pure and easy
    to fuzz-test.
    """
    gender: str = ""
    age: int | None = None
    height_cm: int | None = None
    weight_kg: float | None = None
    activity_coefficient: float = DEFAULT_ACTIVITY
    goal: str = "maintain"
    pace: str = "moderate"
    health_flags: dict = field(default_factory=dict)


@dataclass
class ComputedNorms:
    """Результат расчёта — или ОТКАЗ, у которого все ориентиры ``None``.

    ``int | None``, а не ``int`` с нулём в роли «нет» (§103, вариант A):
    ноль — число, и в арифметике, в JSON и на экране он ведёт себя как
    число. ``None`` ни сложить, ни показать, не заметив, нельзя. Столбцы
    профиля объявлены nullable той же правкой (миграция ``0018``), так что
    отказ доезжает до базы отсутствием, а не нулём.
    """

    bmr: int | None
    daily_kcal: int | None
    daily_protein_g: int | None
    daily_fat_g: int | None
    daily_carbs_g: int | None
    goal: str
    pace: str
    goal_overridden_by: str
    overrides_applied: list[dict[str, Any]] = field(default_factory=list)
    #: Справочный ориентир по жидкости (раздел 4, ``FLUIDS_REFERENCE_ML``).
    #: ``None`` при отказе — вместе со всем остальным.
    daily_water_ml: int | None = None

    # ── Происхождение (DRF-1623 N-d) ────────────────────────────────────
    #
    # Едет ВМЕСТЕ с результатом, а не собирается вызывающей стороной по
    # памяти. Собранное снаружи происхождение — пересказ: оно утверждало
    # бы про расчёт то, что вызывающий о нём думает, а не то, что расчёт
    # сделал. Здесь же снимок собирает та самая функция, которая считала.
    #
    # У ОТКАЗА происхождения нет: при нехватке входов оба поля остаются
    # пустыми, и по ним видно, что ориентира не появилось. Заполнить их
    # на отказе значило бы выдать несостоявшийся расчёт за состоявшийся.
    method_versions: dict[str, str] = field(default_factory=dict)
    input_snapshot: dict[str, Any] = field(default_factory=dict)

    @property
    def computed(self) -> bool:
        """Состоялся ли расчёт. Пустой снимок — расчёта не было."""
        return bool(self.input_snapshot)

    # DRF-265: micronutrient RDA targets (USDA / NIH ODS-derived).
    # Filled by compute_rda(); no override audit because RDA adjustments
    # are deterministic (age/gender/flags-driven).
    #
    # ``None`` на отказе — и у RDA тоже, хотя RDA считается только от пола
    # и возраста: расчёт либо состоялся целиком, либо не состоялся.
    # Половина ориентиров при пустой другой половине выглядела бы как
    # «посчитали, но не всё», а посчитано не было ничего.
    daily_vitamin_d_iu: int | None = None
    daily_vitamin_b12_mcg: float | None = None
    daily_vitamin_c_mg: int | None = None
    daily_iron_mg: float | None = None
    daily_calcium_mg: int | None = None
    daily_magnesium_mg: int | None = None
    daily_omega3_g: float | None = None
    daily_fiber_g: int | None = None


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def _missing_inputs(inputs: ProfileInputs) -> list[str]:
    """Обязательные входы, которых человек не назвал.

    Пустая строка считается пропуском наравне с ``None``: пол хранится
    строкой, и незаполненный он приходит как ``""``.
    """
    missing = []
    for name in REQUIRED_INPUTS:
        value = getattr(inputs, name, None)
        if value is None or value == "":
            missing.append(name)
    return missing


def _input_snapshot(inputs: ProfileInputs, *, goal: str, pace: str) -> dict[str, Any]:
    """Снимок входов состоявшегося расчёта — §85, воспроизводимость.

    Собирается по :data:`SNAPSHOT_INPUTS`, а не перечислением полей
    руками: список один, и он же читается тестом. Перечисленный дважды,
    он разошёлся бы при следующем поле, и разошёлся бы молча.

    ``goal`` и ``pace`` передаются отдельно, потому что к моменту вызова
    лестница переопределений могла их изменить, и в снимок обязано
    попасть то, ПО ЧЕМУ считали.
    """
    snapshot: dict[str, Any] = {}
    for name in SNAPSHOT_INPUTS:
        snapshot[name] = getattr(inputs, name, None)
    snapshot["goal"] = goal
    snapshot["pace"] = pace
    return snapshot


def _health_factors(inputs: ProfileInputs) -> list[str]:
    """Health-факторы, при которых расчёт запрещён, — по именам.

    Флаги читаются по истинности (``{"pregnant": True}``), возраст — по
    порогу ``ADULT_AGE``; неизвестный возраст здесь не фактор (это
    ``insufficient_inputs``). Порядок стабильный: имена в аудите —
    часть контракта.
    """
    flags = inputs.health_flags or {}
    found = [name for name in HEALTH_FACTOR_FLAGS if flags.get(name)]
    if inputs.age is not None and inputs.age < ADULT_AGE:
        found.append(HEALTH_FACTOR_MINOR)
    return found


def _refusal(inputs: ProfileInputs, overrides_applied: list[dict]) -> ComputedNorms:
    """Отказ: все ориентиры ``None``, имя причины — в аудите."""
    return ComputedNorms(
        bmr=None,
        daily_kcal=None,
        daily_protein_g=None,
        daily_fat_g=None,
        daily_carbs_g=None,
        goal=inputs.goal or "maintain",
        pace=inputs.pace or "moderate",
        goal_overridden_by="",
        overrides_applied=overrides_applied,
    )


def compute_norms(inputs: ProfileInputs) -> ComputedNorms:
    """Pure deterministic computation — или ОТКАЗ, если входов не хватает.

    Раньше функция заполняла пропуски медианой пензенской аудитории и
    считала всегда. Теперь пропуск любого из ``REQUIRED_INPUTS`` отменяет
    расчёт целиком: все ориентиры ``None`` и запись в аудите с именем
    отказа.

    ``None``, а не нули (§103, вариант A). Здесь стояли нули с доводом
    «столбцы объявлены ``default=0``, переводить их в nullable — отдельный
    срез». Это тот срез: столбцы nullable (миграция ``0018``), и отказ
    доезжает до базы отсутствием. Ноль был безопасен по уговору («норма
    ноль калорий невозможна»), а уговор — это то, что первый читатель вне
    модуля не знает: снаружи ноль всё равно число.
    """
    # Health-факторы проверяются ПЕРВЫМИ — Safety выше остального (§4
    # владельца: приоритет правил — Safety, затем всё прочее). Отказ с
    # именем на КАЖДЫЙ фактор: «не считаю» без причины читалось бы как
    # «не хватило данных», и человек пошёл бы дополнять анкету.
    factors = _health_factors(inputs)
    if factors:
        return _refusal(inputs, [
            {"reason": f"health_factor_{name}"} for name in factors
        ])

    missing = _missing_inputs(inputs)
    if missing:
        # У пропуска есть ИМЯ и перечень. Молчаливая пустота — отказ
        # без имени: потребитель видит нули и не может отличить «не
        # спросили» от «посчитали и вышло ноль».
        return _refusal(inputs, [{
            "reason": "insufficient_inputs",
            "fields": missing,
        }])

    gender = inputs.gender
    age = inputs.age
    height_cm = inputs.height_cm
    weight_kg = inputs.weight_kg
    assert age is not None and height_cm is not None and weight_kg is not None

    bmr = _mifflin_st_jeor(gender, age, height_cm, weight_kg)
    activity = inputs.activity_coefficient or DEFAULT_ACTIVITY
    goal = inputs.goal or "maintain"
    pace = inputs.pace or "moderate"
    overrides: list[dict] = []
    overridden_by = ""

    # Лестницы «РПП → maintain» и «беременность → maintain + бонус» здесь
    # больше нет: при этих флагах расчёт отказал выше (§5.1). Осталась
    # только нижняя ступень — пол BMR, и это не health-фактор, а граница
    # самого расчёта.

    # BMR floor ladder — only when goal=lose and we'd undercut BMR
    daily_kcal = _kcal_from_goal(bmr, activity, goal, pace)

    if goal == "lose" and daily_kcal < bmr + BMR_FLOOR_MARGIN_KCAL:
        if pace == "moderate":
            overrides.append({
                "reason": "bmr_floor",
                "from": {"pace": "moderate"},
                "to": {"pace": "gentle"},
            })
            pace = "gentle"
            daily_kcal = _kcal_from_goal(bmr, activity, goal, pace)

        if daily_kcal < bmr + BMR_FLOOR_MARGIN_KCAL:
            overrides.append({
                "reason": "bmr_floor",
                "from": {"goal": "lose"},
                "to": {"goal": "maintain"},
            })
            goal = "maintain"
            overridden_by = overridden_by or "bmr_floor"
            daily_kcal = _kcal_from_goal(bmr, activity, goal, pace)

    protein_g, fat_g, carbs_g = _macros_split(daily_kcal, weight_kg, goal)

    # RDA: ветки беременности/кормления внутри ``compute_rda`` до этой точки
    # не доходят — расчёт при этих флагах отказал выше. Флаги передаются как
    # есть, чтобы функция осталась чистой и проверяемой отдельно.
    rda = compute_rda(
        gender=gender,
        age=age,
        health_flags=inputs.health_flags or {},
    )

    return ComputedNorms(
        bmr=int(round(bmr)),
        daily_kcal=int(round(daily_kcal)),
        daily_protein_g=int(round(protein_g)),
        daily_fat_g=int(round(fat_g)),
        daily_carbs_g=int(round(carbs_g)),
        goal=goal,
        pace=pace,
        goal_overridden_by=overridden_by,
        overrides_applied=overrides,
        # Происхождение состоявшегося расчёта. ``goal`` и ``pace`` берутся
        # ПОСЛЕ лестницы переопределений — то есть в снимке лежит то, по
        # чему на самом деле считали, а не то, что человек попросил.
        # Разница между ними уже названа в ``overrides_applied``, и
        # дублировать её снимком значило бы завести второй ответ на один
        # вопрос.
        method_versions={
            "calories": CALORIES_METHOD_VERSION,
            "fluids": FLUIDS_METHOD_VERSION,
        },
        input_snapshot=_input_snapshot(inputs, goal=goal, pace=pace),
        # Раздел 4: справочник по полу. Пол здесь — уже проверенный вход
        # (``REQUIRED_INPUTS``), иначе расчёт отказал бы выше.
        daily_water_ml=FLUIDS_REFERENCE_ML[gender],
        # DRF-265: RDA layer — independent of macro override ladder.
        daily_vitamin_d_iu=rda["vitamin_d_iu"],
        daily_vitamin_b12_mcg=rda["vitamin_b12_mcg"],
        daily_vitamin_c_mg=rda["vitamin_c_mg"],
        daily_iron_mg=rda["iron_mg"],
        daily_calcium_mg=rda["calcium_mg"],
        daily_magnesium_mg=rda["magnesium_mg"],
        daily_omega3_g=rda["omega3_g"],
        daily_fiber_g=rda["fiber_g"],
    )


# ---------------------------------------------------------------------------
# Math helpers
# ---------------------------------------------------------------------------


def _mifflin_st_jeor(gender: str, age: int, height_cm: int, weight_kg: float) -> float:
    base = 10 * weight_kg + 6.25 * height_cm - 5 * age
    return base + (5 if gender == "male" else -161)


def _kcal_from_goal(bmr: float, activity: float, goal: str, pace: str) -> float:
    tdee = bmr * activity
    goal_factor = GOAL_FACTORS.get(goal, 1.0)
    pace_factor = PACE_FACTORS.get(pace, 1.0)
    # Pace softens deficits/surpluses but doesn't move maintain. The
    # multiplicative model: kcal = TDEE × (1 + pace_factor × (goal-1)).
    delta = (goal_factor - 1.0) * pace_factor
    return tdee * (1.0 + delta)


def _macros_split(daily_kcal: float, weight_kg: float, goal: str) -> tuple[float, float, float]:
    """Spec §1.2 implies a goal-aware split. Use a defensible default:
    protein floor 1.6 g/kg (lose/tone), 1.4 g/kg otherwise; fat 30% of
    kcal; carbs fill the remainder.
    """
    protein_per_kg = 1.6 if goal in ("lose", "tone") else 1.4
    protein_g = protein_per_kg * weight_kg
    fat_g = (daily_kcal * 0.30) / 9.0
    used_kcal = protein_g * 4 + fat_g * 9
    carbs_g = max(0.0, (daily_kcal - used_kcal) / 4.0)
    return protein_g, fat_g, carbs_g


# ---------------------------------------------------------------------------
# DRF-265: RDA (recommended daily allowance) for micronutrients
# ---------------------------------------------------------------------------


# Baseline RDA tables. Values from USDA / NIH ODS reference intakes.
# Keyed by ``(gender, age_band)``. Age bands kept coarse on purpose —
# pattern engine doesn't need single-year granularity, and mobile UX
# already only collects an integer year.

# Adolescent: 14-18, Adult: 19-50 (women's iron drops at 50 → menopause),
# Mature: 51-64 (post-menstrual women), Senior: 65+.
_AGE_BANDS = ("adolescent", "adult", "mature", "senior")


def _age_band(age: int) -> str:
    if age < 19:
        return "adolescent"
    if age < 51:
        return "adult"
    if age < 65:
        return "mature"
    return "senior"


_BASE_RDA: dict[tuple[str, str], dict] = {
    # Adult women 19-50.
    ("female", "adult"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 75, "iron_mg": 18,
        "calcium_mg": 1000, "magnesium_mg": 310,
        "omega3_g": 1.1, "fiber_g": 25,
    },
    # Adult men 19-50 — iron drops, magnesium up.
    ("male", "adult"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 90, "iron_mg": 8,
        "calcium_mg": 1000, "magnesium_mg": 400,
        "omega3_g": 1.6, "fiber_g": 38,
    },
    # Adolescent girls 14-18 — calcium up for bone development, iron 15.
    ("female", "adolescent"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 65, "iron_mg": 15,
        "calcium_mg": 1300, "magnesium_mg": 360,
        "omega3_g": 1.1, "fiber_g": 26,
    },
    # Adolescent boys 14-18.
    ("male", "adolescent"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 75, "iron_mg": 11,
        "calcium_mg": 1300, "magnesium_mg": 410,
        "omega3_g": 1.6, "fiber_g": 38,
    },
    # Mature women 51-64 — iron drops to 8 (menopause), bone preservation.
    ("female", "mature"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 75, "iron_mg": 8,
        "calcium_mg": 1200, "magnesium_mg": 320,
        "omega3_g": 1.1, "fiber_g": 21,
    },
    ("male", "mature"): {
        "vitamin_d_iu": 600, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 90, "iron_mg": 8,
        "calcium_mg": 1000, "magnesium_mg": 420,
        "omega3_g": 1.6, "fiber_g": 30,
    },
    # Senior 65+ — vitamin D up to 800, women's calcium 1200.
    ("female", "senior"): {
        "vitamin_d_iu": 800, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 75, "iron_mg": 8,
        "calcium_mg": 1200, "magnesium_mg": 320,
        "omega3_g": 1.1, "fiber_g": 21,
    },
    ("male", "senior"): {
        "vitamin_d_iu": 800, "vitamin_b12_mcg": 2.4,
        "vitamin_c_mg": 90, "iron_mg": 8,
        "calcium_mg": 1200, "magnesium_mg": 420,
        "omega3_g": 1.6, "fiber_g": 30,
    },
}


def compute_rda(
    *, gender: str, age: int, health_flags: dict,
) -> dict[str, Any]:
    """Compute micronutrient RDA targets for the user.

    Pure-math mirror of ``compute_norms`` (kcal/protein/...). Reads
    age + gender as the base index, then layers pregnancy, breastfeeding,
    and dietary flags on top. No LLM, no external calls.
    """
    band = _age_band(age)
    base = dict(_BASE_RDA.get((gender, band), _BASE_RDA[("female", "adult")]))

    # Pregnancy — bumps iron, omega-3, fibre. Calcium stays at 1000
    # (USDA RDA for pregnancy in women 19-50). Pregnancy ≫ flag wins
    # over breastfeeding when both are set (rare but possible).
    if health_flags.get("pregnant"):
        base["iron_mg"] = 27
        base["calcium_mg"] = 1000
        base["omega3_g"] = 1.4
        base["fiber_g"] = 28
        base["vitamin_c_mg"] = 85

    elif health_flags.get("breastfeeding"):
        # Breastfeeding bumps calcium / vit C / omega-3, drops iron
        # (no menstrual loss → 9 mg).
        base["calcium_mg"] = 1000
        base["vitamin_c_mg"] = 120
        base["omega3_g"] = 1.3
        base["iron_mg"] = 9

    # Dietary restriction — vegan multiplier on B12 & iron.
    # Non-heme iron from plants is ~1.8× less bioavailable; B12 doesn't
    # come from plant foods at all, so requirement is ~doubled to push
    # supplementation guidance.
    if health_flags.get("vegan"):
        base["vitamin_b12_mcg"] = 4.0
        base["iron_mg"] = round(base["iron_mg"] * 1.8)
    elif health_flags.get("vegetarian"):
        base["vitamin_b12_mcg"] = 3.0
        # Vegetarians who eat eggs/dairy still get heme-adjacent iron;
        # we apply a milder ×1.3 bump.
        base["iron_mg"] = round(base["iron_mg"] * 1.3, 1)

    return base
