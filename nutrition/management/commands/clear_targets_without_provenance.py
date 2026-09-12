"""Очистка ориентиров без происхождения (§103, срез N-b).

Решение владельца §103 (OD-NUT-5, 11.09.2026), вариант A::

    значение                       NULL
    источник                       none
    данные шести профилей          НЕ считаются согласованными задним числом
    старые ориентиры               очищаются, включая рассчитанные от
                                   подставленных 70 кг
    повторный расчёт               только после согласия или ручной
                                   установки нормы

### Почему команда, а не миграция

Слияние в ``dev`` — это выкладка. Миграция стёрла бы числа живых людей
**в момент слияния**, как побочный эффект работы очереди PR, и человек,
нажавший «слить», не был бы тем, кто решил стереть. Команда разводит два
действия во времени: PR можно слить сегодня, а очистку запустить тогда и
тем, кем решено. Миграция ``0018`` этой же правки — только схемная.

### Почему по умолчанию ничего не делает

Без ``--apply`` команда только ПЕЧАТАЕТ. Необратимое действие требует
отдельного слова, а не отдельного везения.

### Два предмета

1. **``targets_source == unknown_legacy`` — ВСЕ.** Включая тех, у кого
   входы (вес, рост, возраст) на месте. §103 говорит «включая рассчитанные
   от подставленных 70 кг» — включая, а не только: у шести профилей
   пилота происхождение не сохранялось, и различить «посчитано от своего
   веса» и «от подставленного» постфактум нечем (докстринг миграции
   ``0017``). Пересчёт для тех, у кого входы есть, состоится после
   согласия — сторож ``targets_recompute_gate`` это и требует.

2. **``targets_source == none`` с не-NULL ориентиром.** До миграции
   ``0018`` отказ расчёта писал нули, и у строк с ``none`` в столбцах
   лежит ``0``, а не ``NULL``. Ноль — число; §103 требует ``NULL``. Такие
   строки нормализуются той же командой и считаются ОТДЕЛЬНЫМ счётчиком:
   «очищено» и «нормализовано» — разные утверждения.

### Что печатается — значения, а не счётчик

По каждой строке: ``user_id`` (pk — не телефон и не имя), все четырнадцать
ориентиров, ``targets_source``, ``targets_computed_at``. «Очистили шесть»
— утверждение о числе строк; «очистили 2936, 2878, 1590, ...» — о том, что
именно ушло. Второе можно проверить, первое приходится принимать на слово.

Параметры тела (вес, рост, возраст, пол) НЕ печатаются: их печатает
команда §120, а здесь это была бы утечка не по делу. Печатается только
«входы: есть/нет» по каждому полю — этого достаточно, чтобы видеть, кому
пересчёт после согласия возможен, а кому нет.

### Что НЕ трогается — стража, а не обещание

``gender, age, height_cm, weight_kg, weight_range, activity_coefficient,
goal, pace, diet_preference, health_flags, disclaimer_acked, onboarded_at``
и все ``*_at`` (кроме ``updated_at``, который ``auto_now``), а также
``last_overrides_applied`` — аудит прежней лестницы переопределений.
Строки с ``targets_source ∈ {ayla_calculated, user_entered}`` не
трогаются целиком.

Это проверяется ВНУТРИ транзакции перечитыванием из базы: кортеж
нетронутых полей «до» сравнивается с кортежем «после», и расхождение —
исключение и откат. Отдельно перечитывается, что у всех затронутых
четырнадцать столбцов ``IS NULL`` и ``targets_source == none``; иначе
откат. Проверка по данным, а не по собственной работе: ``update_fields``
можно урезать, сигнал ``post_save`` может дописать, — и команда
отчиталась бы успехом за очистку, которой не было.

### Порядок

Эта команда — раньше ``purge_unconsented_body_parameters`` (§120): у той
стоит сторож ``OrderViolation``, требующий ``targets_source == none`` у
всех затронутых. Эта команда делает тот сторож проходимым. Входы она НЕ
трогает — это следующая команда, и об этом печатается строкой в конце.

Usage:
    python manage.py clear_targets_without_provenance
    python manage.py clear_targets_without_provenance --apply
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from nutrition.models import NutritionProfile

#: Четырнадцать столбцов ориентира (§103, часть 1). Список один — он же
#: читается тестом схемы.
TARGET_FIELDS: tuple[str, ...] = (
    "bmr",
    "daily_kcal",
    "daily_protein_g",
    "daily_fat_g",
    "daily_carbs_g",
    "daily_water_ml",
    "daily_vitamin_d_iu",
    "daily_vitamin_b12_mcg",
    "daily_vitamin_c_mg",
    "daily_iron_mg",
    "daily_calcium_mg",
    "daily_magnesium_mg",
    "daily_omega3_g",
    "daily_fiber_g",
)

#: Происхождение, которое очищается ВМЕСТЕ со значением (§92 п.5: значение
#: без происхождения и происхождение без значения одинаково лгут).
PROVENANCE_FIELDS: tuple[str, ...] = (
    "targets_source",
    "targets_method_versions",
    "targets_input_snapshot",
    "targets_computed_at",
)

#: Входы, о которых печатается только «есть/нет». Значения — не здесь.
INPUT_FIELDS: tuple[str, ...] = ("gender", "age", "height_cm", "weight_kg")

#: Что команда НЕ трогает. Сравнивается кортежем до/после внутри
#: транзакции. ``updated_at`` здесь нет — он ``auto_now`` и обязан
#: измениться; ``created_at`` есть.
UNTOUCHED_FIELDS: tuple[str, ...] = (
    "gender",
    "age",
    "height_cm",
    "weight_kg",
    "weight_range",
    "activity_coefficient",
    "goal",
    "pace",
    "diet_preference",
    "health_flags",
    "disclaimer_acked",
    "onboarded_at",
    "first_food_logged_at",
    "weekly_summary_unlocked_at",
    "bmi_warning_overridden_at",
    "created_at",
    "last_overrides_applied",
    "goal_overridden_by",
    "timezone",
)

#: Строки с этим происхождением не трогаются целиком. ``ayla_proposed``
#: (§5.1) — тоже: предложение посчитано с основанием, его судьба —
#: подтверждение человеком или пересчёт, а не команда очистки.
PRESERVED_SOURCES: tuple[str, ...] = (
    NutritionProfile.TargetsSource.AYLA_PROPOSED,
    NutritionProfile.TargetsSource.AYLA_CALCULATED,
    NutritionProfile.TargetsSource.USER_ENTERED,
)


class IncompleteClearing(CommandError):
    """После записи в базе не то, что объявлено, — откат целиком."""


class CollateralWrite(CommandError):
    """Изменилось поле, которое команда обещала не трогать, — откат."""


def _snapshot(p: NutritionProfile, fields: tuple[str, ...]) -> tuple:
    return tuple(getattr(p, f) for f in fields)


def _has_residual_target(p: NutritionProfile) -> bool:
    """Есть ли у строки хоть один не-NULL ориентир."""
    return any(getattr(p, f) is not None for f in TARGET_FIELDS)


def _clear_row(p: NutritionProfile) -> None:
    """Обнулить ориентиры и происхождение ОДНОЙ строки.

    Вынесено в функцию, чтобы тест мог подменить её и доказать, что
    проверка полноты ниже ловит неполную запись, а не доверяет коду.
    """
    for f in TARGET_FIELDS:
        setattr(p, f, None)
    p.targets_source = NutritionProfile.TargetsSource.NONE
    p.targets_method_versions = {}
    p.targets_input_snapshot = {}
    p.targets_computed_at = None
    p.save(update_fields=[*TARGET_FIELDS, *PROVENANCE_FIELDS, "updated_at"])


class Command(BaseCommand):
    help = (
        "Очистить ориентиры без происхождения (§103): unknown_legacy → NULL, "
        "source=none; остатки нулей у source=none → NULL. "
        "Без --apply только печатает."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help=(
                "Выполнить очистку. Без флага команда ничего не меняет — "
                "необратимое действие требует отдельного слова."
            ),
        )

    # ── печать ────────────────────────────────────────────────────────

    def _print_row(self, p: NutritionProfile) -> None:
        values = ", ".join(f"{f}={getattr(p, f)!r}" for f in TARGET_FIELDS)
        inputs = ", ".join(
            f"{f}={'есть' if getattr(p, f) not in (None, '') else 'нет'}"
            for f in INPUT_FIELDS
        )
        self.stdout.write(
            f"  user={p.user_id}  source={p.targets_source}  "
            f"computed_at={p.targets_computed_at!r}"
        )
        self.stdout.write(f"      ориентиры: {values}")
        self.stdout.write(f"      входы: {inputs}")

    # ── исполнение ────────────────────────────────────────────────────

    def handle(self, *args, **options):
        apply = options["apply"]
        Source = NutritionProfile.TargetsSource

        rows = list(NutritionProfile.objects.all().order_by("user_id"))
        legacy = [p for p in rows if p.targets_source == Source.UNKNOWN_LEGACY]
        # Второй предмет: source=none, но в столбцах не NULL (нули до 0018).
        residual = [
            p for p in rows
            if p.targets_source == Source.NONE and _has_residual_target(p)
        ]
        preserved = [p for p in rows if p.targets_source in PRESERVED_SOURCES]

        self.stdout.write(
            f"Профилей всего: {len(rows)}. "
            f"unknown_legacy (очистка): {len(legacy)}. "
            f"none с остатками (нормализация): {len(residual)}. "
            f"ayla_proposed/ayla_calculated/user_entered (не трогаются): {len(preserved)}."
        )
        if not legacy and not residual:
            self.stdout.write(self.style.SUCCESS("Стирать нечего."))
            self._print_footer(preserved)
            return

        verb = "Очищается" if apply else "Будет очищено"
        if legacy:
            self.stdout.write("")
            self.stdout.write(f"{verb} (unknown_legacy → NULL, source=none):")
            for p in legacy:
                self._print_row(p)
        if residual:
            self.stdout.write("")
            self.stdout.write(
                f"{verb} (нормализация: source=none, остатки → NULL):"
            )
            for p in residual:
                self._print_row(p)

        if not apply:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Ничего не изменено. Для очистки: --apply")
            )
            self._print_footer(preserved)
            return

        affected = [*legacy, *residual]
        untouched_before = {
            p.pk: _snapshot(p, UNTOUCHED_FIELDS) for p in affected
        }
        all_fields = (*TARGET_FIELDS, *PROVENANCE_FIELDS, *UNTOUCHED_FIELDS)
        preserved_before = {
            p.pk: _snapshot(p, all_fields) for p in preserved
        }

        with transaction.atomic():
            for p in affected:
                _clear_row(p)

            # ПРОВЕРКА ПОЛНОТЫ — по данным, перечитанным из базы, ВНУТРИ
            # транзакции. ``update_fields`` можно урезать, сигнал может
            # дописать, ``_clear_row`` могут подменить — и без этой
            # проверки команда отчиталась бы успехом за очистку, которой
            # не было. Проверяется то, что ОБЪЯВЛЕНО, а не то, что
            # сделано.
            fresh = {
                p.pk: p
                for p in NutritionProfile.objects.filter(
                    pk__in=[p.pk for p in affected],
                )
            }
            for p in affected:
                row = fresh[p.pk]
                left = [
                    f for f in TARGET_FIELDS if getattr(row, f) is not None
                ]
                if left or row.targets_source != Source.NONE:
                    raise IncompleteClearing(
                        "Очистка не состоялась: у user=%s остались %s, "
                        "targets_source=%s. Транзакция откачена целиком."
                        % (row.user_id, left or "—", row.targets_source)
                    )
                if (
                    row.targets_method_versions
                    or row.targets_input_snapshot
                    or row.targets_computed_at is not None
                ):
                    raise IncompleteClearing(
                        "Очистка не состоялась: у user=%s осталось "
                        "происхождение при пустом значении. Транзакция "
                        "откачена целиком." % row.user_id
                    )
                after = _snapshot(row, UNTOUCHED_FIELDS)
                if after != untouched_before[p.pk]:
                    changed = [
                        f for f, a, b in zip(
                            UNTOUCHED_FIELDS, untouched_before[p.pk], after,
                        )
                        if a != b
                    ]
                    raise CollateralWrite(
                        "Изменилось то, что команда обещала не трогать: "
                        "user=%s поля %s. Транзакция откачена целиком."
                        % (row.user_id, changed)
                    )

            # ПОЛОЖИТЕЛЬНАЯ стража на строки, которые не трогаются: их
            # кортеж «до» равен кортежу «после». Иначе «не трогаем
            # ayla_calculated» — обещание, а обещание никто не проверяет.
            if preserved:
                fresh_preserved = {
                    p.pk: p
                    for p in NutritionProfile.objects.filter(
                        pk__in=[p.pk for p in preserved],
                    )
                }
                for p in preserved:
                    after = _snapshot(fresh_preserved[p.pk], all_fields)
                    if after != preserved_before[p.pk]:
                        raise CollateralWrite(
                            "Строка с источником %s изменилась: user=%s. "
                            "Транзакция откачена целиком."
                            % (p.targets_source, p.user_id)
                        )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"Очищено (unknown_legacy): {len(legacy)}. "
                f"Нормализовано (none, остатки): {len(residual)}."
            )
        )
        self._print_footer(preserved)

    def _print_footer(self, preserved: list[NutritionProfile]) -> None:
        self.stdout.write(
            f"Нетронуто (ayla_proposed/ayla_calculated/user_entered): {len(preserved)}."
        )
        self.stdout.write(
            "Входы (§120/§144) НЕ тронуты — это следующая команда "
            "`purge_unconsented_body_parameters`."
        )
