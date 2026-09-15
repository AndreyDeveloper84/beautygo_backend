"""DRF-1922 — таксономия пилота: у варианта target и action_type (H5), тройка с family (I1).

Написана руками: оба поля обязательны и без умолчания — значения для
существующих вариантов нет и **быть не должно** (вариант бывает только у
NBA-исхода, и тройка у него обязательна). Как 0002/0003, безопасна только на
пустой таблице вариантов — это проверяет первая операция, а не описание.
Наборы без вариантов (SAFETY_BOUNDARY) цели не несут, поэтому считаются варианты.
Замер пилота перед слиянием — главное окно.

``preserve_default=False`` — умолчание живёт только в этой миграции (для пустой
таблицы оно не применяется ни к одной строке) и в модель не попадает: значение
обязан назвать ``persist``.
"""
from django.db import migrations, models


def refuse_if_records_exist(apps, schema_editor):
    Recommendation = apps.get_model("recommendation", "Recommendation")
    existing = Recommendation.objects.count()
    if existing:
        raise RuntimeError(
            f"DRF-1922: в recommendation_recommendation {existing} строк. Миграция добавляет обязательные "
            "target и action_type (H5) и безопасна только на пустой таблице вариантов (записи immutable, "
            "значений для существующих вариантов нет). Остановлено до изменения схемы."
        )


TARGET_CHOICES = [
    ("FACE_FRESHNESS", "FACE_FRESHNESS"),
    ("PUFFINESS_REDUCTION", "PUFFINESS_REDUCTION"),
    ("RELAXATION", "RELAXATION"),
    ("BACK_COMFORT", "BACK_COMFORT"),
]
ACTION_TYPE_CHOICES = [
    ("PROVIDER_SESSION", "PROVIDER_SESSION"),
    ("SELF_CARE", "SELF_CARE"),
    ("OBSERVE", "OBSERVE"),
    ("PLAN", "PLAN"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("recommendation", "0003_context_snapshot"),
    ]

    operations = [
        migrations.RunPython(refuse_if_records_exist, migrations.RunPython.noop),
        migrations.AddField(
            model_name="recommendation",
            name="target",
            field=models.CharField(choices=TARGET_CHOICES, default="", max_length=32),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="recommendation",
            name="action_type",
            field=models.CharField(choices=ACTION_TYPE_CHOICES, default="", max_length=24),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="recommendation",
            name="record_schema_version",
            field=models.CharField(default="1.3", max_length=8),
        ),
        migrations.AlterField(
            model_name="recommendationset",
            name="record_schema_version",
            field=models.CharField(default="1.3", max_length=8),
        ),
    ]
