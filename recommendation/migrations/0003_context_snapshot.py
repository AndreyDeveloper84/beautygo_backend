"""DRF-1906 — снимок контекста решения: строка ContextSnapshot и FK набора вместо JSON-ссылки.

Написана руками: FK набора обязателен и без умолчания — значения для
существующих строк нет и **быть не должно**. Как и 0002, безопасна только на
пустой таблице наборов (пилот: 0 строк, бот не пишет до 6.4) — это проверяет
первая операция, а не описание.

Одна правда: JSON ``context_snapshot_ref`` уходит — ссылкой можно было назвать
несуществующий снимок; FK на строку снимка так не может.
"""
import uuid

import django.db.models.deletion
from django.db import migrations, models


def refuse_if_records_exist(apps, schema_editor):
    RecommendationSet = apps.get_model("recommendation", "RecommendationSet")
    existing = RecommendationSet.objects.count()
    if existing:
        raise RuntimeError(
            f"DRF-1906: в recommendation_recommendationset {existing} строк. Миграция заменяет JSON-ссылку "
            "на снимок обязательным FK на строку снимка и безопасна только на пустой таблице (записи "
            "immutable, снимков для существующих наборов нет). Остановлено до изменения схемы."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("recommendation", "0002_set_level_outcome"),
    ]

    operations = [
        migrations.RunPython(refuse_if_records_exist, migrations.RunPython.noop),
        migrations.CreateModel(
            name="ContextSnapshot",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("subject_ref", models.CharField(max_length=64)),
                ("snapshot_version", models.CharField(max_length=32)),
                ("content_digest", models.CharField(max_length=64)),
                ("content", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField()),
                ("erased_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={
                "indexes": [models.Index(fields=["subject_ref", "created_at"], name="ctxsnap_subject_created_idx")],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(erased_at__isnull=True) | models.Q(content={}),
                        name="ctxsnap_erased_has_empty_content",
                    ),
                ],
            },
        ),
        migrations.RemoveField(model_name="recommendationset", name="context_snapshot_ref"),
        migrations.AddField(
            model_name="recommendationset",
            name="context_snapshot",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="recommendation_sets",
                to="recommendation.contextsnapshot",
            ),
        ),
        migrations.AlterField(
            model_name="recommendation",
            name="record_schema_version",
            field=models.CharField(default="1.2", max_length=8),
        ),
        migrations.AlterField(
            model_name="recommendationset",
            name="record_schema_version",
            field=models.CharField(default="1.2", max_length=8),
        ),
    ]
