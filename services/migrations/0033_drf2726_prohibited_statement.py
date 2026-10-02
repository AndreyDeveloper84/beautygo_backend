"""У запрещённого утверждения есть предмет (DRF-2726, решение владельца 02.10, блок C).

Три шага, порядок существенен:

1. колонка ``prohibited_statement`` у возможности и у связи с целью;
2. подтверждённые запреты без предмета возвращаются в черновик (правило и
   обоснование — в ``_drf2726_prohibition_demote``, там же оно проверяется
   тестом): колонка только что появилась, и любой запрет, подтверждённый
   раньше, новое ограничение нарушает;
3. ограничения: предмет запрета — только у ``prohibited_claim``, а у
   подтверждённого запрета обязателен.

Обратного действия у шага 2 нет намеренно.
"""
from django.conf import settings
from django.db import migrations, models

from . import _drf2726_prohibition_demote as prohibition_demote


def _demote(apps, schema_editor):
    counts = prohibition_demote.demote(
        apps.get_model("services", "ProcedureCapability"),
        apps.get_model("services", "CapabilityGoalLink"),
    )
    print(
        f"  approved prohibition without a statement -> draft: "
        f"capabilities={counts['capabilities']} goal_links={counts['goal_links']}"
    )


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0032_drf2726_result_timeframe_ground"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="capabilitygoallink",
            name="prohibited_statement",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="procedurecapability",
            name="prohibited_statement",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.RunPython(_demote, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="capabilitygoallink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("prohibited_statement", ""),
                    ("claim_scope", "prohibited_claim"),
                    _connector="OR",
                ),
                name="capabilitygoallink_prohibition_only_on_prohibited_claim",
            ),
        ),
        migrations.AddConstraint(
            model_name="capabilitygoallink",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    models.Q(("claim_scope", "prohibited_claim"), _negated=True),
                    models.Q(("prohibited_statement", ""), _negated=True),
                    _connector="OR",
                ),
                name="capabilitygoallink_approved_prohibition_has_statement",
            ),
        ),
        migrations.AddConstraint(
            model_name="procedurecapability",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("prohibited_statement", ""),
                    ("claim_scope", "prohibited_claim"),
                    _connector="OR",
                ),
                name="procedurecapability_prohibition_only_on_prohibited_claim",
            ),
        ),
        migrations.AddConstraint(
            model_name="procedurecapability",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "approved"), _negated=True),
                    models.Q(("claim_scope", "prohibited_claim"), _negated=True),
                    models.Q(("prohibited_statement", ""), _negated=True),
                    _connector="OR",
                ),
                name="procedurecapability_approved_prohibition_has_statement",
            ),
        ),
    ]
