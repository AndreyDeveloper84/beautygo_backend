# DRF-2279 (CD §76, №32): пометка прежних умолчаний темпа и активности.
# Аддитивная: новый столбец с пустым списком, данные не трогаются — метит
# команда ``mark_legacy_default_inputs`` (без ``--apply`` только печатает).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("nutrition", "0027_activity_not_invented_q59"),
    ]

    operations = [
        migrations.AddField(
            model_name="nutritionprofile",
            name="legacy_default_inputs",
            field=models.JSONField(blank=True, default=list),
        ),
    ]
