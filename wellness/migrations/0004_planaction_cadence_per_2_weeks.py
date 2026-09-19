# DRF-2123 (§51) — каденс per_2_weeks: только choices/help_text, схема не меняется.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wellness", "0003_plan_lite_goal_and_book_service"),
    ]

    operations = [
        migrations.AlterField(
            model_name="planaction",
            name="cadence",
            field=models.CharField(
                choices=[
                    ("per_day", "Раз в день"),
                    ("per_week", "Раз в неделю"),
                    ("per_2_weeks", "Раз в две недели"),
                ],
                max_length=16,
            ),
        ),
        migrations.AlterField(
            model_name="planaction",
            name="target_count",
            field=models.PositiveSmallIntegerField(
                default=1,
                help_text="Сколько раз за ведро каденса (день для per_day, неделя для per_week, 14 дней для per_2_weeks)",
            ),
        ),
    ]
