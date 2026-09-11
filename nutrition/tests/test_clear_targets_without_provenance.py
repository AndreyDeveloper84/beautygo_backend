"""Очистка ориентиров без происхождения: схема, команда, порядок (§103).

Решение владельца §103 (OD-NUT-5), вариант A: значение ``NULL``, источник
``none``, старые ориентиры очищаются ВСЕ, пересчёт — только после
согласия. Здесь закреплены:

1. схема — четырнадцать столбцов nullable, новая строка их не имеет;
2. команда — сухой прогон печатает и не пишет, ``--apply`` пишет ровно
   объявленное, второй запуск — «Стирать нечего»;
3. полнота — неполная запись ловится перечитыванием и откатывается;
4. порядок двух команд — сторож ``OrderViolation`` у §120 становится
   проходимым ровно после этой команды;
5. дневник не зависит от ориентиров (§109);
6. воспроизводимость расчёта от снимка входов (§85).
"""

from __future__ import annotations

from datetime import datetime, timezone as dt_tz
from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

from nutrition.management.commands import clear_targets_without_provenance as cmd
from nutrition.management.commands.purge_unconsented_body_parameters import (
    OrderViolation,
)
from nutrition.models import NutritionProfile
from nutrition.serializers import NutritionSummaryResponseSerializer
from nutrition.services.nutrition_profile_service import (
    ProfileInputs,
    compute_norms,
)
from users.models import User

pytestmark = pytest.mark.django_db

Source = NutritionProfile.TargetsSource

#: Замер пилота 11.09.2026 — профиль с полными входами. Значения
#: ориентиров — те, что лежат в базе (посчитаны до провенанса).
PILOT_FULL = dict(
    gender="male", age=41, height_cm=185, weight_kg=95.0,
    activity_coefficient=1.4, goal="maintain", pace="moderate",
    bmr=1906, daily_kcal=2936, daily_protein_g=133, daily_fat_g=98,
    daily_carbs_g=380, daily_water_ml=2850,
    daily_vitamin_d_iu=600, daily_vitamin_b12_mcg=2.4, daily_vitamin_c_mg=90,
    daily_iron_mg=8.0, daily_calcium_mg=1000, daily_magnesium_mg=400,
    daily_omega3_g=1.6, daily_fiber_g=38,
)

#: Профиль пилота, у которого входов нет, а ориентиры посчитаны от
#: подставленных 70 кг: вода 2100 = 30 × 70.
PILOT_ASSUMED = dict(
    gender="female", age=None, height_cm=None, weight_kg=None,
    activity_coefficient=1.4, goal="lose", pace="moderate",
    bmr=1370, daily_kcal=1918, daily_protein_g=112, daily_fat_g=64,
    daily_carbs_g=224, daily_water_ml=2100,
    daily_vitamin_d_iu=600, daily_vitamin_b12_mcg=2.4, daily_vitamin_c_mg=75,
    daily_iron_mg=18.0, daily_calcium_mg=1000, daily_magnesium_mg=310,
    daily_omega3_g=1.1, daily_fiber_g=25,
)


def _user(name):
    return User.objects.create(username=name, role="client", is_proxy=True)


def _profile(name, source, **over):
    fields = dict(user=_user(name), targets_source=source)
    fields.update(over)
    return NutritionProfile.objects.create(**fields)


def _run(*args):
    out = StringIO()
    call_command("clear_targets_without_provenance", *args, stdout=out)
    return out.getvalue()


def _targets(p):
    return tuple(getattr(p, f) for f in cmd.TARGET_FIELDS)


def _untouched(p):
    return tuple(getattr(p, f) for f in cmd.UNTOUCHED_FIELDS)


# ===========================================================================
# 1. Схема
# ===========================================================================


class TestSchemaTargetsAreNullable:
    def test_all_fourteen_target_columns_accept_null(self):
        """§103 вариант A: «значение NULL». Все четырнадцать, не выборочно."""
        for f in cmd.TARGET_FIELDS:
            field = NutritionProfile._meta.get_field(f)
            assert field.null is True, f"{f}: null={field.null}"
            assert field.default is None, f"{f}: default={field.default!r}"

    def test_a_fresh_profile_has_no_targets_at_all(self):
        """Новая строка ориентира не имеет — и это видно самой строке."""
        p = _profile("schema-fresh", Source.NONE)
        p.refresh_from_db()
        assert _targets(p) == (None,) * len(cmd.TARGET_FIELDS)
        assert p.targets_source == Source.NONE

    def test_the_fourteen_are_exactly_the_brief_list(self):
        expected = {
            "bmr", "daily_kcal", "daily_protein_g", "daily_fat_g",
            "daily_carbs_g", "daily_water_ml", "daily_vitamin_d_iu",
            "daily_vitamin_b12_mcg", "daily_vitamin_c_mg", "daily_iron_mg",
            "daily_calcium_mg", "daily_magnesium_mg", "daily_omega3_g",
            "daily_fiber_g",
        }
        assert set(cmd.TARGET_FIELDS) == expected
        assert len(cmd.TARGET_FIELDS) == 14


# ===========================================================================
# 3. Сухой прогон
# ===========================================================================


class TestDryRun:
    def test_prints_values_and_changes_nothing(self):
        legacy = _profile("dry-legacy", Source.UNKNOWN_LEGACY, **PILOT_FULL)
        before_targets = _targets(legacy)
        before_untouched = _untouched(legacy)

        out = _run()

        legacy.refresh_from_db()
        assert _targets(legacy) == before_targets
        assert _untouched(legacy) == before_untouched
        assert legacy.targets_source == Source.UNKNOWN_LEGACY
        # ЗНАЧЕНИЯ, а не счётчик: сами числа в выводе.
        assert f"user={legacy.user_id}" in out
        assert "daily_kcal=2936" in out
        assert "daily_water_ml=2850" in out
        assert "bmr=1906" in out
        assert "source=unknown_legacy" in out
        assert "Ничего не изменено" in out
        assert "--apply" in out

    def test_prints_input_presence_but_never_input_values(self):
        """Вес/рост/возраст печатает команда §120, здесь — только есть/нет."""
        _profile("dry-inputs", Source.UNKNOWN_LEGACY, **PILOT_FULL)
        _profile("dry-noinputs", Source.UNKNOWN_LEGACY, **PILOT_ASSUMED)

        out = _run()

        assert "weight_kg=есть" in out
        assert "weight_kg=нет" in out
        assert "age=есть" in out and "age=нет" in out
        # Само значение веса/роста/возраста в выводе не встречается.
        assert "95.0" not in out
        assert "185" not in out
        assert "=41" not in out

    def test_nothing_to_clear_when_no_legacy_and_no_residue(self):
        _profile("dry-calc", Source.AYLA_CALCULATED, **PILOT_FULL)
        _profile("dry-none", Source.NONE)

        out = _run()

        assert "Стирать нечего." in out
        assert "Нетронуто (ayla_calculated/user_entered): 1." in out


# ===========================================================================
# 4. --apply
# ===========================================================================


class TestApply:
    def test_clears_legacy_rows_and_preserves_everything_else(self):
        legacy_full = _profile(
            "apply-full", Source.UNKNOWN_LEGACY,
            last_overrides_applied=[{"reason": "bmr_floor"}],
            health_flags={"vegetarian": True},
            disclaimer_acked={"ts": "2026-01-01", "version": "1"},
            onboarded_at=datetime(2026, 1, 2, tzinfo=dt_tz.utc),
            targets_method_versions={"calories": "old"},
            targets_input_snapshot={"weight_kg": 95.0},
            targets_computed_at=datetime(2026, 1, 3, tzinfo=dt_tz.utc),
            **PILOT_FULL,
        )
        legacy_assumed = _profile(
            "apply-assumed", Source.UNKNOWN_LEGACY, **PILOT_ASSUMED,
        )
        calc = _profile(
            "apply-calc", Source.AYLA_CALCULATED,
            targets_method_versions={"calories": "mifflin_st_jeor_v1"},
            targets_input_snapshot={"weight_kg": 95.0},
            targets_computed_at=datetime(2026, 9, 1, tzinfo=dt_tz.utc),
            **PILOT_FULL,
        )
        entered = _profile(
            "apply-entered", Source.USER_ENTERED, daily_kcal=1800,
        )
        untouched_before = {
            p.pk: _untouched(p) for p in (legacy_full, legacy_assumed)
        }
        calc_before = (_targets(calc), calc.targets_source,
                       calc.targets_method_versions, calc.targets_computed_at)
        entered_before = (_targets(entered), entered.targets_source)

        out = _run("--apply")

        for p in (legacy_full, legacy_assumed):
            p.refresh_from_db()
            assert _targets(p) == (None,) * 14, p.user.username
            assert p.targets_source == Source.NONE
            assert p.targets_method_versions == {}
            assert p.targets_input_snapshot == {}
            assert p.targets_computed_at is None
            assert _untouched(p) == untouched_before[p.pk], p.user.username
        # Входы на месте — их стирает §120, не эта команда.
        assert legacy_full.weight_kg == 95.0
        assert legacy_full.height_cm == 185
        assert legacy_full.age == 41
        assert legacy_full.gender == "male"
        # Аудит прежней лестницы — на месте.
        assert legacy_full.last_overrides_applied == [{"reason": "bmr_floor"}]

        calc.refresh_from_db()
        assert (
            _targets(calc), calc.targets_source,
            calc.targets_method_versions, calc.targets_computed_at,
        ) == calc_before
        entered.refresh_from_db()
        assert (_targets(entered), entered.targets_source) == entered_before

        assert "Очищено (unknown_legacy): 2." in out
        assert "Нормализовано (none, остатки): 0." in out
        assert "Нетронуто (ayla_calculated/user_entered): 2." in out
        assert (
            "Входы (§120/§144) НЕ тронуты — это следующая команда "
            "`purge_unconsented_body_parameters`." in out
        )
        # Значения напечатаны ДО записи — после их неоткуда взять.
        assert "daily_kcal=2936" in out
        assert "daily_water_ml=2100" in out

    def test_normalises_zero_residue_on_none_rows_as_a_separate_count(self):
        """Второй предмет: нули у source=none (до 0018) → NULL, свой счётчик."""
        residue = _profile(
            "apply-residue", Source.NONE,
            bmr=0, daily_kcal=0, daily_protein_g=0, daily_water_ml=2100,
            last_overrides_applied=[{
                "reason": "insufficient_inputs", "fields": ["weight_kg"],
            }],
        )
        clean = _profile("apply-clean-none", Source.NONE)

        out = _run("--apply")

        residue.refresh_from_db()
        assert _targets(residue) == (None,) * 14
        assert residue.targets_source == Source.NONE
        assert residue.last_overrides_applied == [{
            "reason": "insufficient_inputs", "fields": ["weight_kg"],
        }]
        clean.refresh_from_db()
        assert _targets(clean) == (None,) * 14
        assert "Очищено (unknown_legacy): 0." in out
        assert "Нормализовано (none, остатки): 1." in out
        assert "daily_water_ml=2100" in out

    def test_second_run_has_nothing_to_clear(self):
        _profile("apply-twice", Source.UNKNOWN_LEGACY, **PILOT_FULL)
        _run("--apply")

        out = _run("--apply")

        assert "Стирать нечего." in out


# ===========================================================================
# 5. Полнота — по данным, не по коду
# ===========================================================================


class TestIncompleteClearingRollsBack:
    def test_a_column_left_behind_raises_and_rolls_back(self):
        """Подмена ``_clear_row`` оставляет один столбец — команда бросает,
        база как до запуска. Без перечитывания внутри транзакции этот же
        прогон отчитался бы успехом."""
        legacy = _profile("partial", Source.UNKNOWN_LEGACY, **PILOT_FULL)
        before = (_targets(legacy), legacy.targets_source)

        def half_clear(p):
            for f in cmd.TARGET_FIELDS:
                if f != "daily_kcal":
                    setattr(p, f, None)
            p.targets_source = Source.NONE
            p.targets_method_versions = {}
            p.targets_input_snapshot = {}
            p.targets_computed_at = None
            p.save()

        with patch.object(cmd, "_clear_row", half_clear):
            with pytest.raises(cmd.IncompleteClearing) as exc:
                _run("--apply")

        assert "daily_kcal" in str(exc.value)
        legacy.refresh_from_db()
        assert (_targets(legacy), legacy.targets_source) == before

    def test_source_left_behind_raises_and_rolls_back(self):
        legacy = _profile("partial-src", Source.UNKNOWN_LEGACY, **PILOT_FULL)
        before = (_targets(legacy), legacy.targets_source)

        def values_only(p):
            for f in cmd.TARGET_FIELDS:
                setattr(p, f, None)
            p.save(update_fields=[*cmd.TARGET_FIELDS, "updated_at"])

        with patch.object(cmd, "_clear_row", values_only):
            with pytest.raises(cmd.IncompleteClearing):
                _run("--apply")

        legacy.refresh_from_db()
        assert (_targets(legacy), legacy.targets_source) == before

    def test_a_collateral_write_raises_and_rolls_back(self):
        """Стража на нетронутое — не обещание: тронули вес — откат."""
        legacy = _profile("collateral", Source.UNKNOWN_LEGACY, **PILOT_FULL)

        real = cmd._clear_row

        def clear_and_touch_weight(p):
            real(p)
            p.weight_kg = 70.0
            p.save(update_fields=["weight_kg"])

        with patch.object(cmd, "_clear_row", clear_and_touch_weight):
            with pytest.raises(cmd.CollateralWrite) as exc:
                _run("--apply")

        assert "weight_kg" in str(exc.value)
        legacy.refresh_from_db()
        assert legacy.weight_kg == 95.0
        assert legacy.daily_kcal == 2936
        assert legacy.targets_source == Source.UNKNOWN_LEGACY


# ===========================================================================
# 6. Порядок исполнения двух команд
# ===========================================================================


class TestOrderOfTheTwoCommands:
    def test_purge_refuses_before_clear_and_passes_after(self):
        """Между очисткой и удалением входов нет запрещённого окна.

        До этой команды §120 бросает ``OrderViolation`` (ориентир пережил
        бы входы — §92 п.5). После неё — проходит: вес NULL, ориентиры
        NULL, source none.
        """
        legacy = _profile("order", Source.UNKNOWN_LEGACY, **PILOT_FULL)

        with pytest.raises(OrderViolation):
            call_command(
                "purge_unconsented_body_parameters", "--apply",
                stdout=StringIO(),
            )
        legacy.refresh_from_db()
        assert legacy.weight_kg == 95.0
        assert legacy.daily_kcal == 2936

        _run("--apply")
        legacy.refresh_from_db()
        assert legacy.targets_source == Source.NONE
        assert legacy.weight_kg == 95.0  # входы ещё на месте

        call_command(
            "purge_unconsented_body_parameters", "--apply", stdout=StringIO(),
        )
        legacy.refresh_from_db()
        assert legacy.weight_kg is None
        assert legacy.height_cm is None
        assert legacy.age is None
        assert _targets(legacy) == (None,) * 14
        assert legacy.targets_source == Source.NONE


# ===========================================================================
# 9. Дневник не зависит от ориентиров (§109)
# ===========================================================================


class TestDiaryDoesNotDependOnTargets:
    def test_food_and_water_log_and_summary_work_with_null_targets(self):
        """Стража на регрессию: NULL в ориентирах не ломает дневник.

        Читается СЕРИАЛИЗОВАННЫЙ ответ (через ``OmitAbsentTargetsMixin``),
        а не dataclass: ключи ``calories_goal``/``water_goal_ml`` должны
        отсутствовать в JSON, а не быть ``None`` в объекте.
        """
        from nutrition.services.food_log_service import (
            CreateFoodLogInput,
            FoodLogService,
        )
        from nutrition.services.nutrition_summary_service import (
            NutritionSummaryService,
        )
        from nutrition.services.water_entry_service import (
            CreateWaterInput,
            WaterEntryService,
        )

        profile = _profile("diary", Source.NONE, goal="lose")
        assert _targets(profile) == (None,) * 14
        user = profile.user
        now = datetime.now(dt_tz.utc)

        log = FoodLogService().create(CreateFoodLogInput(
            user_id=user.id, dish_name="Борщ", portion_multiplier=1.0,
            meal_type="lunch", logged_at=now,
        ))
        assert log.calories > 0
        water = WaterEntryService().create(CreateWaterInput(
            user_id=user.id, ml=250, beverage_slug=None, ts=now,
            idempotency_key=None,
        ))
        assert water.ml == 250
        assert water.today_norm_water_ml is None

        # День — ОТ ТОГО ЖЕ ``now``, что и запись, и в той же зоне, в которой
        # сводка режет сутки (``datetime.combine(day, …, tzinfo=utc)``).
        # ``date.today()`` здесь красило dev три часа в сутки: Django ставит
        # ``TZ = settings.TIME_ZONE`` (Europe/Moscow), и с 21:00 UTC
        # локальная дата уже «завтра», а запись лежит во «вчера» по UTC —
        # сводка за «сегодня» находила ноль (прогон dev c40666f4, 00:00 MSK).
        # Пояс прогона — молчаливый параметр; тест обязан расходиться при
        # любом пересчёте, а не совпадать по удаче.
        summary = NutritionSummaryService().summary(
            user_id=user.id, day=now.date(),
        )
        data = NutritionSummaryResponseSerializer(summary).data
        assert data["calories_total"] == pytest.approx(log.calories)
        assert len(data["entries"]) == 1
        assert "calories_goal" not in data
        assert "water_goal_ml" not in data
        # Вода v3 живёт в своей таблице (``WaterEntry``) и своей ручке:
        # запись видна там, ориентира нет и там.
        today = WaterEntryService().today(user.id)
        assert today.today_total_water_ml == 250
        assert today.today_norm_water_ml is None

        progressive = NutritionSummaryService().progressive(user=user, period=7)
        assert progressive.period == 7
        # goal=lose, но ориентира нет — блока цели нет, а не блок с None.
        assert progressive.goal_progress is None


# ===========================================================================
# 10. Воспроизводимость (§85)
# ===========================================================================


class TestReproducibility:
    def test_pilot_inputs_give_the_pilot_bmr(self):
        """male/41/185/95: 10·95 + 6.25·185 − 5·41 + 5 = 1906.25 → 1906."""
        norms = compute_norms(ProfileInputs(
            gender="male", age=41, height_cm=185, weight_kg=95.0,
            activity_coefficient=1.4, goal="maintain", pace="moderate",
        ))
        assert norms.bmr == 1906

    def test_recompute_from_snapshot_reproduces_all_fourteen(self):
        first = compute_norms(ProfileInputs(
            gender="female", age=36, height_cm=170, weight_kg=67.0,
            activity_coefficient=1.4, goal="lose", pace="moderate",
        ))
        assert first.computed
        snapshot = dict(first.input_snapshot)

        again = compute_norms(ProfileInputs(**snapshot))
        third = compute_norms(ProfileInputs(**snapshot))

        for f in cmd.TARGET_FIELDS:
            if f == "daily_water_ml":
                continue  # ориентира по жидкости нет ни у кого (§82)
            assert getattr(again, f) == getattr(first, f), f
            assert getattr(third, f) == getattr(first, f), f
        assert again.method_versions == first.method_versions
        assert again.input_snapshot == snapshot
        assert third.input_snapshot == snapshot
