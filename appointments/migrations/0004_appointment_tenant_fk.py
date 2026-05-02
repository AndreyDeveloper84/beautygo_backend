"""Add tenant FK to Appointment (DRF-242.3).

Pure additive — null=True, no backfill yet (DRF-242.4). PROTECT so a
mistakenly-deleted Tenant doesn't orphan booking history.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0003_alter_payment_amount"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="appointment",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="appointments",
                to="tenants.tenant",
            ),
        ),
    ]
