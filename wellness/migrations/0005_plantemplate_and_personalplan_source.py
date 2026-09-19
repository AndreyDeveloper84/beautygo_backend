# DRF-2123 (§51, План-A) — шаблоны Plan Lite как данные + источник плана.

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wellness", "0004_planaction_cadence_per_2_weeks"),
    ]

    operations = [
        migrations.AddField(
            model_name="personalplan",
            name="source",
            field=models.CharField(
                default="manual",
                help_text="manual | template:<goal_key>:v<N> — по какому шаблону составлен (Plan Lite)",
                max_length=96,
            ),
        ),
        migrations.CreateModel(
            name="PlanTemplate",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4, editable=False, primary_key=True, serialize=False,
                    ),
                ),
                (
                    "goal_key",
                    models.SlugField(
                        help_text="Ключ курируемой цели (services.GoalOption.key)", max_length=64,
                    ),
                ),
                (
                    "actions",
                    models.JSONField(
                        default=list,
                        help_text="[{action_type, cadence, target_count}] — 1–3 обязательства в форме PlanAction",
                    ),
                ),
                (
                    "why_text",
                    models.TextField(
                        help_text="Слово владельца «почему такой план» — показывается человеку дословно",
                    ),
                ),
                ("version", models.PositiveIntegerField(default=1)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["goal_key", "-version"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("is_active", True)),
                        fields=("goal_key",),
                        name="plantemplate_one_active_per_goal_key",
                    ),
                    models.UniqueConstraint(
                        fields=("goal_key", "version"),
                        name="plantemplate_goal_key_version_unique",
                    ),
                ],
            },
        ),
    ]
