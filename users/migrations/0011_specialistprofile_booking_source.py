"""Add booking_source + yclients_company_id to SpecialistProfile (#439).

Purely additive — both fields ship with defaults that match the current
implicit behavior (every existing specialist is 'ayla_local'; no YClients
integration code exists yet). No two-step needed; no read/write switch
required.

ADR-0009 §Booking SoR rule:
- 'ayla_local'  → Ayla djangoproject is the system of record (current
                   default).
- 'yclients'    → YClients is the system of record; booking flow goes
                   through their API and mirrors locally.

Phase 1.5+ may move these fields to a ProviderLocation table if multi-
location providers appear. Documented in
docs/architecture/booking-source-dual-mode.md.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0010_user_specialistprofile_tenant_indexes'),
    ]

    operations = [
        migrations.AddField(
            model_name='specialistprofile',
            name='booking_source',
            field=models.CharField(
                choices=[
                    ('ayla_local', 'Ayla local DB SoR'),
                    ('yclients', 'YClients SoR'),
                ],
                default='ayla_local',
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name='specialistprofile',
            name='yclients_company_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    "YClients account/company id — required when "
                    "booking_source='yclients', empty otherwise."
                ),
                max_length=64,
            ),
        ),
    ]
