# DRF-2124 (План-B, В-4) — подсказка анкете питания под курируемую цель как
# данные шаблона. Существующие строки получают [] («без подсказки»); сид
# seed_plan_templates положит значения таблицы §51 новой версией.

import wellness.models
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("wellness", "0005_plantemplate_and_personalplan_source"),
    ]

    operations = [
        migrations.AddField(
            model_name="plantemplate",
            name="nutrition_goal_hint",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Подсказка анкете питания: [] | [lose|maintain|gain, …] — что обычно выбирают под цель",
                validators=[wellness.models.validate_nutrition_goal_hint],
            ),
        ),
    ]
