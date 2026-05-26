"""Add yclients_staff_id to SpecialistProfile (#92, W1 batch lookup).

Per-master identifier inside a YClients account — distinct from the
account-level ``yclients_company_id`` already on the model. Required to
resolve YClients staff ids (sent by W1's admin mini-app) into Ayla
SpecialistProfile rows.

Purely additive: blank default keeps existing rows valid. Indexed
because the W1 batch lookup endpoint filters by it.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0012_tenantuserrelationship'),
    ]

    operations = [
        migrations.AddField(
            model_name='specialistprofile',
            name='yclients_staff_id',
            field=models.CharField(
                blank=True,
                db_index=True,
                default='',
                help_text=(
                    "YClients per-master staff id — required when "
                    "booking_source='yclients' and the admin mini-app "
                    "needs to resolve YClients staff ids → Ayla "
                    "SpecialistProfile rows. Empty otherwise."
                ),
                max_length=64,
            ),
        ),
    ]
