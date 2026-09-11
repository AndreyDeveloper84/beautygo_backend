"""Ориентиры становятся nullable: «ориентира нет» — это NULL, а не ноль.

DRF-1623, срез N-b. Решение владельца §103 (OD-NUT-5), вариант A,
11.09.2026::

    target = NULL   source = none              значения нет
    target = 1800   source = manual            задано человеком
    target = 2100   source = ayla_calculated   посчитано

## Что делает

Четырнадцать столбцов ориентиров ``NutritionProfile`` (``bmr``,
``daily_kcal``, макросы, вода, восемь RDA) получают ``null=True,
default=None``. Больше ничего.

## Чего НЕ делает — и это важнее

**Значения существующих строк не трогает.** Шесть профилей пилота с
``targets_source = unknown_legacy`` после этой миграции лежат ровно так,
как лежали: те же числа, тот же источник.

Слияние в ``dev`` — это выкладка. Миграция, стирающая данные, стёрла бы
их **в момент слияния**, как побочный эффект работы очереди PR, и
человек, нажавший «слить», не был бы тем, кто решил удалить. Довод
целиком — в докстринге ``purge_unconsented_body_parameters``; здесь он
исполняется тем же способом: очистка вынесена в команду
``clear_targets_without_provenance`` с сухим прогоном по умолчанию.

## Почему default=None, а не сохранить default=0

Ноль был уговором: «дневная норма ноль килокалорий физически невозможна,
значит ноль читается как отсутствие». Уговор держался внутри модуля и
ломался снаружи — в JSON ноль ЧИСЛО, и «0 из 0 · 0 %» на экране уже
случалось (§82). Новая строка профиля ориентира не имеет, и это должно
быть видно самой строке, а не читателю, знающему уговор.

Обратный ход (``unapply``) сохранит существующие NULL как... не сохранит:
``PositiveIntegerField(default=0)`` без ``null`` при откате потребует
значение для каждой NULL-строки, и Django подставит default. Откат этой
миграции ПОСЛЕ запуска команды очистки — это не откат схемы, а повторное
воскрешение нулей; делать его нельзя, и это названо здесь, а не открыто
на месте.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("nutrition", "0017_target_provenance_legacy_backfill"),
    ]

    operations = [
        migrations.AlterField(
            model_name="nutritionprofile",
            name="bmr",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_calcium_mg",
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_carbs_g",
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_fat_g",
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_fiber_g",
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_iron_mg",
            field=models.FloatField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_kcal",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_magnesium_mg",
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_omega3_g",
            field=models.FloatField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_protein_g",
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_vitamin_b12_mcg",
            field=models.FloatField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_vitamin_c_mg",
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_vitamin_d_iu",
            field=models.PositiveSmallIntegerField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name="nutritionprofile",
            name="daily_water_ml",
            field=models.PositiveIntegerField(blank=True, default=None, null=True),
        ),
    ]
