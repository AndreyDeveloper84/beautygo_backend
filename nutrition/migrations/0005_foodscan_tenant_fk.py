"""Add tenant FK to FoodScan (DRF-242.3).

Pure additive — null=True, no backfill yet (DRF-242.4). PROTECT prevents
orphan scans on accidental tenant deletion.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("nutrition", "0004_foodlog_idempotency"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="foodscan",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="food_scans",
                to="tenants.tenant",
            ),
        ),
    ]
