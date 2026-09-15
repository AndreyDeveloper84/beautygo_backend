"""DRF-1905 — исход на уровне набора (§32): primary только при NBA.

Написана руками, а не ``makemigrations``: колонкам набора без умолчания нужны
значения для существующих строк, а их **быть не должно**. Условие главного
окна 15.09 — «миграция immutable-моделей безопасна, пока записей нет:
вызывающих persist на пилоте 0, бот не зовёт до 6.4» — проверяет первая
операция, а не описание: при хотя бы одном наборе миграция отказывает.

``preserve_default=False`` у новых обязательных полей — умолчание живёт только
в этой миграции (для пустой таблицы оно не применяется ни к одной строке) и не
попадает в модель: значение обязан назвать ``persist``.
"""
import django.db.models.deletion
from django.db import migrations, models


def refuse_if_records_exist(apps, schema_editor):
    RecommendationSet = apps.get_model("recommendation", "RecommendationSet")
    existing = RecommendationSet.objects.count()
    if existing:
        raise RuntimeError(
            f"DRF-1905: в recommendation_recommendationset {existing} строк. Миграция переносит исход, "
            "готовность, безопасность, снимок и версии с записи на набор и безопасна только на пустой "
            "таблице (записи immutable, перенос значений не предусмотрен). Остановлено до изменения схемы."
        )


RESULT_STATUS_CHOICES = [
    ("CLEAR_PRIMARY", "CLEAR_PRIMARY"),
    ("MULTIPLE_SUITABLE", "MULTIPLE_SUITABLE"),
    ("INSUFFICIENT_CONTEXT", "INSUFFICIENT_CONTEXT"),
    ("SAFETY_BOUNDARY", "SAFETY_BOUNDARY"),
    ("NO_ACTION", "NO_ACTION"),
]
READINESS_STATE_CHOICES = [
    ("READY", "READY"),
    ("NEEDS_DISCRIMINATION", "NEEDS_DISCRIMINATION"),
    ("NEEDS_REQUIRED_CONTEXT", "NEEDS_REQUIRED_CONTEXT"),
    ("INSUFFICIENT_EVIDENCE", "INSUFFICIENT_EVIDENCE"),
    ("BLOCKED", "BLOCKED"),
]
VERSION_FIELDS = (
    "decision_policy_version",
    "taxonomy_version",
    "safety_policy_version",
    "catalog_mapping_version",
    "presentation_policy_version",
)


class Migration(migrations.Migration):

    dependencies = [
        ("recommendation", "0001_recommendation_record"),
    ]

    operations = [
        migrations.RunPython(refuse_if_records_exist, migrations.RunPython.noop),
        # --- с записи уходит то, что одно на проход ----------------------------
        migrations.RemoveField(model_name="recommendation", name="result_status"),
        migrations.RemoveField(model_name="recommendation", name="readiness_state"),
        migrations.RemoveField(model_name="recommendation", name="safety_evaluation_ref"),
        migrations.RemoveField(model_name="recommendation", name="context_snapshot_ref"),
        *[migrations.RemoveField(model_name="recommendation", name=name) for name in VERSION_FIELDS],
        migrations.AlterField(
            model_name="recommendation",
            name="record_schema_version",
            field=models.CharField(default="1.1", max_length=8),
        ),
        # --- набор получает исход прохода --------------------------------------
        migrations.AddField(
            model_name="recommendationset",
            name="result_status",
            field=models.CharField(choices=RESULT_STATUS_CHOICES, default="", max_length=24),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="recommendationset",
            name="readiness_state",
            field=models.CharField(choices=READINESS_STATE_CHOICES, default="", max_length=24),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="recommendationset", name="reason_codes", field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="recommendationset", name="evidence_refs", field=models.JSONField(default=list),
        ),
        migrations.AddField(
            model_name="recommendationset", name="explanation", field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="recommendationset", name="safety_evaluation_ref", field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="recommendationset", name="context_snapshot_ref", field=models.JSONField(default=dict),
        ),
        migrations.AddField(
            model_name="recommendationset",
            name="primary",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="recommendation.recommendation",
            ),
        ),
        *[
            migrations.AddField(
                model_name="recommendationset",
                name=name,
                field=models.CharField(default="", max_length=32),
                preserve_default=False,
            )
            for name in VERSION_FIELDS
        ],
        migrations.AddField(
            model_name="recommendationset",
            name="record_schema_version",
            field=models.CharField(default="1.1", max_length=8),
        ),
        migrations.AddConstraint(
            model_name="recommendationset",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("result_status__in", ["INSUFFICIENT_CONTEXT", "SAFETY_BOUNDARY"]), ("primary__isnull", True))
                    | models.Q(
                        models.Q(("result_status__in", ["INSUFFICIENT_CONTEXT", "SAFETY_BOUNDARY"]), _negated=True),
                        ("primary__isnull", False),
                    )
                ),
                name="recset_primary_iff_nba_status",
            ),
        ),
    ]
