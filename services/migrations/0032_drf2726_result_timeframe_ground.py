"""Срок результата — словами и с основанием (DRF-2726, решение владельца 02.10, блок B).

Три шага, порядок существенен:

1. колонка ``ProcedureCapability.variability_note`` — оговорка о разбросе;
2. подтверждённые строки, которые новые ограничения не пропустили бы,
   возвращаются в черновик (правило и обоснование — в
   ``_drf2726_timeframe_demote``, там же оно проверяется тестом);
3. ограничения: у подтверждённой строки срок — словами и с основанием.

Без шага 2 одна такая строка на стенде остановила бы ``migrate``, а с ним и
запуск сайта. Черновики база не судит и шаг их не трогает.

Обратного действия у шага 2 нет намеренно: откат схемы уносит колонку и
ограничения, а «подтверждать» строки обратно миграция не вправе.
"""
from django.conf import settings
from django.db import migrations, models

from . import _drf2726_timeframe_demote as timeframe_demote


def _demote(apps, schema_editor):
    counts = timeframe_demote.demote(
        apps.get_model("services", "ProcedureCapability"),
        apps.get_model("services", "CapabilityGoalLink"),
    )
    print(
        f"  approved timeframe without a ground -> draft: "
        f"capabilities={counts['capabilities']} goal_links={counts['goal_links']}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0031_drf2721_unlink_recharge_from_medical_category"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="procedurecapability",
            name="variability_note",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(_demote, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="capabilitygoallink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    ("result_horizon", ""),
                    models.Q(
                        models.Q(("variability_note", ""), _negated=True),
                        models.Q(("evidence_source", ""), _negated=True),
                        models.Q(("source_ref", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="capabilitygoallink_approved_horizon_grounded",
            ),
        ),
        migrations.AddConstraint(
            model_name="capabilitygoallink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    ("result_horizon", ""),
                    models.Q(
                        ("result_horizon__regex", "^[^A-Za-zА-Яа-яЁё]*$"), _negated=True
                    ),
                    _connector="OR",
                ),
                name="capabilitygoallink_approved_horizon_in_words",
            ),
        ),
        migrations.AddConstraint(
            model_name="procedurecapability",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    ("result_timeframe", ""),
                    models.Q(
                        models.Q(("variability_note", ""), _negated=True),
                        models.Q(("evidence_source", ""), _negated=True),
                        models.Q(("source_ref", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="procedurecapability_approved_timeframe_grounded",
            ),
        ),
        migrations.AddConstraint(
            model_name="procedurecapability",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    ("result_timeframe", ""),
                    models.Q(
                        ("result_timeframe__regex", "^[^A-Za-zА-Яа-яЁё]*$"),
                        _negated=True,
                    ),
                    _connector="OR",
                ),
                name="procedurecapability_approved_timeframe_in_words",
            ),
        ),
    ]
