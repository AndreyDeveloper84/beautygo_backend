"""По-видовому происхождению — честное имя, а не копия общей подписи.

DRF-1929, F1(б). Дополняет схемную ``0022``, где колонки заведены как
``NULL`` без умолчания.

## Решение владельца (16.09.2026)

* стратегия — ``UNKNOWN_LEGACY`` / ``UNKNOWN_LEGACY``;
* **старый ``targets_source`` НЕ копировать** в ``calories_source`` и
  ``fluids_source``;
* новое происхождение — всегда по видам;
* точечный исторический backfill — **только при доказанном authoritative
  evidence**, отдельно от этой миграции.

## Что делает

У каждого вида своя проверка — по НАЛИЧИЮ ЧИСЛА этого вида, ровно как
``0017`` отбирала строки по ``daily_kcal > 0``:

* есть число вида → ``unknown_legacy`` («происхождение не сохранялось»);
* числа вида нет → ``none`` («ориентира нет» — нечему принадлежать).

Значения ориентиров не меняются: ни одно число не гасится и не
пересчитывается.

## Почему не копировать ``targets_source``

Соблазн понятен: у строки с ``user_entered`` происхождение как будто
известно. Но общая подпись говорит о НАБОРЕ, а не о виде, и ровно из-за
этого F1(б) и заводился: ручная правка калорий делала ``user_entered``
весь набор, включая справочную воду, которую человек не называл.
Скопировать её по видам значило бы размножить исходную ошибку и выдать
предположение за факт — то самое, от чего отказалась ``0017``.

``unknown_legacy`` здесь работает и как защита: читатели считают его
недействующим (``CONFIRMED_SOURCES``), поэтому остаток снятой формулы
``30 × вес`` в ``daily_water_ml`` (§82) наружу не уедет.

## Точечный backfill — что может им стать

Аудит ручного ввода (``last_overrides_applied``) хранит записи вида
``{"reason": "user_entered", "fields": ["daily_kcal", ...]}`` — то есть
ПОИМЁННО, какие числа называл человек. Это кандидат в authoritative
evidence по видам, и по решению владельца он идёт ОТДЕЛЬНЫМ предметом:
здесь его нет намеренно. Сколько строк такую запись несут — замер по
контуру, не вывод из кода.

## Обратная операция

Возвращает обе колонки в ``NULL`` — состояние, которое создала ``0022``:
«по видам не устанавливалось». Прежние значения общей подписи не
затрагиваются ни вперёд, ни назад.
"""

from django.db import migrations

UNKNOWN_LEGACY = "unknown_legacy"
NONE = "none"


def kind_backfill_value(has_value: bool) -> str:
    """Что получает вид: имя происхождения по НАЛИЧИЮ числа этого вида.

    Вынесено отдельной функцией намеренно: правило отбора — предмет
    решения владельца, и оно должно проверяться тестом напрямую. В
    репозитории нет ни ``django_test_migrations``, ни иного способа
    прогнать миграцию локально (единственный executor-тест — только под
    Postgres и вне его молча пропускается), поэтому тестируется ПРАВИЛО,
    а не применение миграции; это названный предел покрытия.
    """
    return UNKNOWN_LEGACY if has_value else NONE


def name_per_kind_provenance(apps, schema_editor):
    NutritionProfile = apps.get_model("nutrition", "NutritionProfile")

    # Калории: число есть — ``daily_kcal > 0`` (тот же отбор, что в 0017).
    NutritionProfile.objects.filter(daily_kcal__gt=0).update(
        calories_source=kind_backfill_value(True),
    )
    NutritionProfile.objects.exclude(daily_kcal__gt=0).update(
        calories_source=kind_backfill_value(False),
    )

    # Жидкость: число есть — ``daily_water_ml > 0``. Столбец nullable
    # (§103), поэтому NULL и ноль оба означают «числа нет».
    NutritionProfile.objects.filter(daily_water_ml__gt=0).update(
        fluids_source=kind_backfill_value(True),
    )
    NutritionProfile.objects.exclude(daily_water_ml__gt=0).update(
        fluids_source=kind_backfill_value(False),
    )


def unname(apps, schema_editor):
    NutritionProfile = apps.get_model("nutrition", "NutritionProfile")
    NutritionProfile.objects.update(calories_source=None, fluids_source=None)


class Migration(migrations.Migration):

    dependencies = [
        ("nutrition", "0022_nutritionprofile_calories_confirmed_at_and_more"),
    ]

    operations = [
        migrations.RunPython(name_per_kind_provenance, unname),
    ]
