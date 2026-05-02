"""Add tenant FK to User and SpecialistProfile (DRF-242.3).

Pure additive — both fields nullable, no backfill in this step. Backfill
+ feature flag rollout happen in DRF-242.4. PROTECT semantics so a
mistakenly-deleted Tenant doesn't orphan auth records.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0007_user_is_proxy"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="users",
                to="tenants.tenant",
            ),
        ),
        migrations.AddField(
            model_name="specialistprofile",
            name="tenant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.PROTECT,
                related_name="specialist_profiles",
                to="tenants.tenant",
            ),
        ),
    ]
