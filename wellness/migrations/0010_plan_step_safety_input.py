"""DRF-2877 (Plan WP3 §2) — безопасность хода на строках перехода шага и связи с записью.

По три колонки в ``PlanStepResolution`` и ``PlanStepBooking``: состояние,
версия политики, ревизия состояния разговора. Только схема.

Умолчания ниже — разовые, для ``ADD COLUMN NOT NULL``: таблицы созданы
миграцией ``0009`` и до этого листа писателей с боевым вызывающим не имели.
Значение ``UNKNOWN`` выбрано намеренно: если строка всё же существует, о ней
честно сказано «при каком состоянии безопасности действовали — неизвестно», а
не подставлено ``NORMAL``. В модели умолчаний нет: писатель обязан назвать все
три поля.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wellness", "0009_plan_step_resolution_and_booking"),
    ]

    operations = [
        migrations.AddField(
            model_name="planstepresolution",
            name="safety_state",
            field=models.CharField(default="UNKNOWN", max_length=16),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="planstepresolution",
            name="safety_policy_version",
            field=models.CharField(default="unknown", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="planstepresolution",
            name="safety_evaluated_at_revision",
            field=models.PositiveIntegerField(default=0),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="planstepbooking",
            name="safety_state",
            field=models.CharField(default="UNKNOWN", max_length=16),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="planstepbooking",
            name="safety_policy_version",
            field=models.CharField(default="unknown", max_length=64),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="planstepbooking",
            name="safety_evaluated_at_revision",
            field=models.PositiveIntegerField(default=0),
            preserve_default=False,
        ),
    ]
