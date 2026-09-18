"""Расчёт норм по утверждённой методике §85 — ``mifflin_st_jeor_v2`` (DRF-2097).

Решение владельца 18.09 (§48 п.3): методика §85 утверждена. Что стережётся:

* a1/a2 — коэффициент активности только из набора 1.2 / 1.375 / 1.55 / 1.725:
  чужое значение (умолчание схемы 1.4, которое шлёт анкета бота) подводится к
  ближайшему — 1.375, при равном расстоянии к меньшему, — со следом
  ``activity_normalised`` и нормализованным значением в снимке; значение из
  набора идёт как есть, следа нет;
* g1 — поправка на цель не более ±10 % (свойство на сетке входов × цель ×
  темп) — раньше ``lose`` был −20 %; g2 — −20 % не встречается ни при какой
  цели; t3 — отклонение от поддержания > 30 % на расчётном пути недостижимо
  по построению (тем самым «неблокирующее подтверждение» OD-NUT-3 расчёту
  не нужно);
* r1 — ``daily_kcal`` кратен 10, макросы считаны от округлённого числа;
* t1 — ниже 1000 ккал — отказ по имени, upsert не сохраняет ни значения, ни
  происхождения; t2 — 1000–1199 — сохраняется со следом ``calories_low``;
* v1 — версия ``mifflin_st_jeor_v2`` в ``method_versions``; профиль с ``v1``
  без нового расчёта остаётся ``v1``, после upsert — ``v2``;
* e1 — лестница BMR floor работает с новыми факторами;
* c1 — перепись: факторы цели внутри ±10 %, набор активности ровно четыре,
  пороги OD-NUT-3 — одним источником (в ``manual_targets_service`` литералов
  1000/1200/0.30 нет).

Ссылки: `docs/decisions/AYLA_NUTRITION_TARGETS_ARCHITECTURE_DECISION.md`
разделы 3.3 и 7.2; OPEN_DECISIONS §85.
"""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from nutrition.models import NutritionProfile
from nutrition.services import manual_targets_service
from nutrition.services import nutrition_profile_service as nps
from nutrition.services.nutrition_profile_service import (
    CALORIES_METHOD_VERSION,
    GOAL_FACTORS,
    PACE_FACTORS,
    ProfileInputs,
    compute_norms,
)

# Имена, заведённые этим листом, берутся с умолчаниями §85: так файл
# собирается и на базе без правки, и красное видно по узлам, а не ошибкой
# сбора. Умолчания равны решению владельца — на правильном коде они
# совпадают с модулем (c1 это утверждает).
ACTIVITY_COEFFICIENTS = getattr(nps, "ACTIVITY_COEFFICIENTS", (1.2, 1.375, 1.55, 1.725))
CALORIES_HARD_FLOOR_KCAL = getattr(nps, "CALORIES_HARD_FLOOR_KCAL", 1000)
CALORIES_WARN_BELOW_KCAL = getattr(nps, "CALORIES_WARN_BELOW_KCAL", 1200)
GOAL_FACTOR_LIMIT = getattr(nps, "GOAL_FACTOR_LIMIT", 0.10)
KCAL_ROUNDING = getattr(nps, "KCAL_ROUNDING", 10)
MAINTENANCE_DEVIATION_RATIO = getattr(nps, "MAINTENANCE_DEVIATION_RATIO", 0.30)
REFUSAL_CALORIES_BELOW_FLOOR = getattr(nps, "REFUSAL_CALORIES_BELOW_FLOOR", "calories_below_floor")
WARN_CALORIES_LOW = getattr(nps, "WARN_CALORIES_LOW", "calories_low")

CONSENT = {"type": "personal_calculation", "document_version": "v1"}

#: Сетка входов — взрослые, без health-факторов; на ней все узлы-свойства.
GRID = [
    ProfileInputs(gender=g, age=a, height_cm=h, weight_kg=w, activity_coefficient=act)
    for g in ("female", "male")
    for a in (20, 35, 55)
    for h in (155, 170, 185)
    for w in (48.0, 65.0, 90.0)
    for act in ACTIVITY_COEFFICIENTS
]


def _with(base: ProfileInputs, **over) -> ProfileInputs:
    return ProfileInputs(**{**base.__dict__, **over})


def _maintenance(base: ProfileInputs) -> int:
    norms = compute_norms(_with(base, goal="maintain", pace="moderate"))
    assert norms.computed and norms.daily_kcal is not None
    return norms.daily_kcal


# ─── активность ─────────────────────────────────────────────────────────────


class TestActivitySet:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(1.4, 1.375), (1.4625, 1.375), (1.0, 1.2), (2.5, 1.725), (1.6, 1.55), (None, 1.375)],
    )
    def test_a1_out_of_set_is_normalised_to_the_nearest_lower_on_ties(self, raw, expected):
        norms = compute_norms(
            ProfileInputs(gender="female", age=30, height_cm=165, weight_kg=60, activity_coefficient=raw)
        )

        assert norms.computed
        trail = [o for o in norms.overrides_applied if o["reason"] == "activity_normalised"]
        assert trail == [{
            "reason": "activity_normalised",
            "from": {"activity_coefficient": float(raw or 1.4)},
            "to": {"activity_coefficient": expected},
        }]
        assert norms.input_snapshot["activity_coefficient"] == expected  # снимок — по чему считали

    @pytest.mark.parametrize("coefficient", ACTIVITY_COEFFICIENTS)
    def test_a2_a_value_from_the_set_is_used_as_is(self, coefficient):
        norms = compute_norms(
            ProfileInputs(gender="male", age=40, height_cm=180, weight_kg=80, activity_coefficient=coefficient)
        )
        assert norms.computed
        assert [o for o in norms.overrides_applied if o["reason"] == "activity_normalised"] == []
        assert norms.input_snapshot["activity_coefficient"] == coefficient
        # maintain: daily = BMR × коэффициент, округлённое до 10 (bmr в DTO сам округлён до 1).
        assert abs(norms.daily_kcal - norms.bmr * coefficient) <= KCAL_ROUNDING / 2 + 1


# ─── поправка на цель ───────────────────────────────────────────────────────


class TestGoalAdjustment:
    def test_g1_every_goal_and_pace_stays_within_ten_percent_of_maintenance(self):
        seen = 0
        worst = 0.0
        for base, goal, pace in itertools.product(GRID, GOAL_FACTORS, PACE_FACTORS):
            norms = compute_norms(_with(base, goal=goal, pace=pace))
            if not norms.computed:
                continue  # отказ порога — предмет t1, не этого узла
            maintenance = _maintenance(base)
            # Поправка ≤ 10 % применяется к неокруглённому числу; округление до
            # 10 ккал обеих величин добавляет не больше 2 × KCAL_ROUNDING.
            tolerance = GOAL_FACTOR_LIMIT + 2 * KCAL_ROUNDING / maintenance
            ratio = abs(norms.daily_kcal - maintenance) / maintenance
            worst = max(worst, ratio)
            assert ratio <= tolerance, (base, goal, pace, norms.daily_kcal, maintenance)
            seen += 1
        assert seen >= 500  # нижняя граница: сетка не пуста

    def test_g2_a_twenty_percent_deficit_never_happens(self):
        base = ProfileInputs(gender="male", age=30, height_cm=180, weight_kg=85, activity_coefficient=1.55)
        maintenance = _maintenance(base)
        for goal, pace in itertools.product(GOAL_FACTORS, PACE_FACTORS):
            norms = compute_norms(_with(base, goal=goal, pace=pace))
            assert norms.computed
            assert norms.daily_kcal > maintenance * 0.85, (goal, pace, norms.daily_kcal, maintenance)
        # положительная стража: lose всё же ниже поддержания
        lose = compute_norms(_with(base, goal="lose", pace="moderate"))
        assert lose.daily_kcal < maintenance

    def test_t3_a_thirty_percent_deviation_is_unreachable_by_construction(self):
        """OD-NUT-3 «>30 % — неблокирующее подтверждение» расчёту не нужно: узел, не код."""
        worst = 0.0
        for base, goal, pace in itertools.product(GRID[::7], GOAL_FACTORS, PACE_FACTORS):
            norms = compute_norms(_with(base, goal=goal, pace=pace))
            if norms.computed:
                maintenance = _maintenance(base)
                worst = max(worst, abs(norms.daily_kcal - maintenance) / maintenance)
        assert 0 < worst < MAINTENANCE_DEVIATION_RATIO


# ─── округление и пороги ───────────────────────────────────────────────────


class TestRoundingAndThresholds:
    def test_r1_daily_kcal_is_a_multiple_of_ten_and_macros_follow_it(self):
        from nutrition.services.nutrition_profile_service import _macros_split

        checked = 0
        for base in GRID[::5]:
            norms = compute_norms(_with(base, goal="lose", pace="gentle"))
            if not norms.computed:
                continue
            assert norms.daily_kcal % KCAL_ROUNDING == 0, norms.daily_kcal
            p, f, c = _macros_split(float(norms.daily_kcal), base.weight_kg, "lose")
            assert (norms.daily_protein_g, norms.daily_fat_g, norms.daily_carbs_g) == (
                int(round(p)), int(round(f)), int(round(c)),
            )
            checked += 1
        assert checked >= 30

    def test_t1_below_the_floor_is_a_named_refusal_with_nothing_saved(self, django_user_model):
        from nutrition.services.profile_upsert_service import upsert_profile

        # Поддержание само ниже порога (BMR 775 × 1.2 = 930): лестница BMR floor
        # здесь не поможет — это цель maintain, отступать некуда.
        inputs = ProfileInputs(
            gender="female", age=70, height_cm=145, weight_kg=38, activity_coefficient=1.2, goal="maintain"
        )
        norms = compute_norms(inputs)
        assert not norms.computed and norms.daily_kcal is None and norms.bmr is None
        refusal = [o for o in norms.overrides_applied if o["reason"] == REFUSAL_CALORIES_BELOW_FLOOR]
        assert len(refusal) == 1 and refusal[0]["daily_kcal"] < CALORIES_HARD_FLOOR_KCAL
        assert norms.method_versions == {} and norms.input_snapshot == {}

        user = django_user_model.objects.create(username="drf2097-floor")
        body = upsert_profile(
            user=user, external_user_id="bot:max:2097001", idempotency_key=None,
            payload={
                "consent": CONSENT, "gender": "female", "age": 70, "height_cm": 145,
                "weight_kg": 38, "activity_coefficient": 1.2, "goal": "maintain",
            },
        )
        profile = NutritionProfile.objects.get(user=user)
        assert body["norms"] == {} and profile.daily_kcal is None
        assert profile.targets_source == NutritionProfile.TargetsSource.NONE
        assert profile.targets_method_versions == {}
        assert REFUSAL_CALORIES_BELOW_FLOOR in [o["reason"] for o in body["overrides_applied"]]

    def test_t2_the_warning_window_is_saved_with_a_named_trail(self, django_user_model):
        from nutrition.services.profile_upsert_service import upsert_profile

        # BMR 896 × 1.2 = 1075.8 → 1080: в окне предупреждения.
        inputs = ProfileInputs(
            gender="female", age=60, height_cm=150, weight_kg=42, activity_coefficient=1.2, goal="maintain"
        )
        norms = compute_norms(inputs)
        assert norms.computed
        assert CALORIES_HARD_FLOOR_KCAL <= norms.daily_kcal < CALORIES_WARN_BELOW_KCAL, norms.daily_kcal
        assert {"reason": WARN_CALORIES_LOW, "daily_kcal": norms.daily_kcal} in norms.overrides_applied

        user = django_user_model.objects.create(username="drf2097-warn")
        body = upsert_profile(
            user=user, external_user_id="bot:max:2097002", idempotency_key=None,
            payload={
                "consent": CONSENT, "gender": "female", "age": 60, "height_cm": 150,
                "weight_kg": 42, "activity_coefficient": 1.2, "goal": "maintain",
            },
        )
        profile = NutritionProfile.objects.get(user=user)
        assert profile.daily_kcal == norms.daily_kcal and body["norms"]["daily_kcal"] == norms.daily_kcal
        assert WARN_CALORIES_LOW in [o["reason"] for o in profile.last_overrides_applied]
        assert profile.targets_method_versions["calories"] == CALORIES_METHOD_VERSION


# ─── версия ────────────────────────────────────────────────────────────────


class TestMethodVersion:
    def test_v1_the_version_is_v2_and_an_old_profile_is_not_recomputed_silently(self, django_user_model):
        from nutrition.services.profile_upsert_service import upsert_profile

        assert CALORIES_METHOD_VERSION == "mifflin_st_jeor_v2"
        user = django_user_model.objects.create(username="drf2097-v1")
        profile = NutritionProfile.objects.create(
            user=user, gender="female", age=30, height_cm=165, weight_kg=60,
            activity_coefficient=1.4, goal="lose", daily_kcal=1450,
            # Строка до этого листа: происхождение v1, предложение не обновлялось.
            # Гейт пересчёта (§103 N-b) без основания её не трогает — v1 остаётся.
            targets_source=NutritionProfile.TargetsSource.UNKNOWN_LEGACY,
            targets_method_versions={"calories": "mifflin_st_jeor_v1", "fluids": "adult_beverages_reference_v1"},
            targets_input_snapshot={"gender": "female", "age": 30, "height_cm": 165, "weight_kg": 60,
                                    "activity_coefficient": 1.4, "goal": "lose", "pace": "moderate"},
        )
        # Без основания для пересчёта (нет attestation) — старое число и старая версия остаются.
        upsert_profile(user=user, external_user_id="bot:max:2097003", idempotency_key=None,
                       payload={"diet_preference": "none"})
        profile.refresh_from_db()
        assert profile.daily_kcal == 1450
        assert profile.targets_method_versions["calories"] == "mifflin_st_jeor_v1"

        # Следующий расчёт с основанием — новая версия, новое число.
        upsert_profile(user=user, external_user_id="bot:max:2097003", idempotency_key=None,
                       payload={"consent": CONSENT, "weight_kg": 61})
        profile.refresh_from_db()
        assert profile.targets_method_versions["calories"] == "mifflin_st_jeor_v2"
        assert profile.daily_kcal is not None and profile.daily_kcal % KCAL_ROUNDING == 0
        assert profile.targets_input_snapshot["activity_coefficient"] == 1.375  # нормализовано


# ─── лестница и перепись ───────────────────────────────────────────────────


class TestLadderAndCensus:
    def test_e1_the_bmr_floor_ladder_still_works_with_the_new_factors(self):
        """−10 % при активности 1.2 ниже BMR + 100: лестница уводит в gentle, затем в maintain."""
        norms = compute_norms(
            ProfileInputs(
                gender="female", age=30, height_cm=160, weight_kg=55, activity_coefficient=1.2, goal="lose"
            )
        )
        assert norms.computed
        # Первая ступень: moderate → gentle хватает (−9.2 % ≥ BMR + 100).
        assert [o["reason"] for o in norms.overrides_applied] == ["bmr_floor"]
        assert (norms.goal, norms.pace) == ("lose", "gentle")
        assert norms.daily_kcal >= norms.bmr + 100 - KCAL_ROUNDING

        # Вторая ступень: и gentle ниже пола → цель уходит в maintain.
        small = compute_norms(
            ProfileInputs(
                gender="female", age=50, height_cm=150, weight_kg=45, activity_coefficient=1.2, goal="lose"
            )
        )
        assert small.computed
        assert [o["reason"] for o in small.overrides_applied][:2] == ["bmr_floor", "bmr_floor"]
        assert small.goal == "maintain" and small.goal_overridden_by == "bmr_floor"
        assert small.daily_kcal >= small.bmr + 100 - KCAL_ROUNDING

    def test_c1_the_constants_obey_the_decision(self):
        for name in (
            "ACTIVITY_COEFFICIENTS", "CALORIES_HARD_FLOOR_KCAL", "CALORIES_WARN_BELOW_KCAL",
            "GOAL_FACTOR_LIMIT", "KCAL_ROUNDING", "MAINTENANCE_DEVIATION_RATIO",
            "REFUSAL_CALORIES_BELOW_FLOOR", "WARN_CALORIES_LOW",
        ):
            assert hasattr(nps, name), f"{name} не объявлен в nutrition_profile_service"
        assert tuple(sorted(ACTIVITY_COEFFICIENTS)) == (1.2, 1.375, 1.55, 1.725)
        for goal, factor in GOAL_FACTORS.items():
            assert abs(factor - 1.0) <= GOAL_FACTOR_LIMIT + 1e-9, (goal, factor)
        for pace, factor in PACE_FACTORS.items():
            assert 0 < factor <= 1.0, (pace, factor)  # темп только смягчает дельту
        assert (CALORIES_HARD_FLOOR_KCAL, CALORIES_WARN_BELOW_KCAL, MAINTENANCE_DEVIATION_RATIO) == (1000, 1200, 0.30)
        # Один источник порогов: manual_targets_service их импортирует, а не объявляет.
        assert manual_targets_service.CALORIES_HARD_FLOOR_KCAL is CALORIES_HARD_FLOOR_KCAL
        source = Path(manual_targets_service.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        declared = {
            t.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Name)
        }
        assert not declared & {"CALORIES_HARD_FLOOR_KCAL", "CALORIES_WARN_BELOW_KCAL", "MAINTENANCE_DEVIATION_RATIO"}


pytestmark = pytest.mark.django_db
