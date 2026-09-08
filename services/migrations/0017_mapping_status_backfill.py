"""Существующие связи получают статус — и ни одна не получает `VERIFIED`.

Четвёртый шаг последовательности владельца (`OPEN_DECISIONS.md` §76).
Правило и его обоснование — в `_mapping_status_backfill`, там же оно
и проверяется тестом; здесь только запуск.

Обратного действия у миграции нет намеренно. Откат, возвращающий всё
в `unmapped`, стёр бы решения, принятые людьми после её применения, —
а откат схемы (0016) и так уносит колонку целиком. Обратимость,
уничтожающая данные, хуже необратимости, которая честно об этом
говорит.
"""
from django.db import migrations

from . import _mapping_status_backfill as backfill


def _forward(apps, schema_editor):
    salon_service = apps.get_model("services", "SalonService")
    counts = backfill.classify(salon_service)
    print(
        f"  mapping_status: review_required={counts['review_required']} "
        f"unmapped={counts['unmapped']} verified=0"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0016_salonservice_mapping_status"),
    ]

    operations = [
        migrations.RunPython(_forward, migrations.RunPython.noop),
    ]
