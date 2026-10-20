"""DRF-2984 (owner 20.10): salon — 990 ₽ for up to 3 masters, +150 ₽ for
each billable master above that; solo has no extra price (a second
master converts the cabinet to salon, В-2).

Updates the rows 0002 seeded; creates none, so tests that count tariff
rows see the same two.
"""
from decimal import Decimal

from django.db import migrations

SALON_EXTRA_MASTER_PRICE = Decimal("150.00")


def seed_extra_master_price(apps, schema_editor):
    TariffPlan = apps.get_model("billing", "TariffPlan")
    TariffPlan.objects.filter(code="salon", extra_master_price__isnull=True).update(
        extra_master_price=SALON_EXTRA_MASTER_PRICE,
    )


def unseed_extra_master_price(apps, schema_editor):
    TariffPlan = apps.get_model("billing", "TariffPlan")
    TariffPlan.objects.filter(code="salon").update(extra_master_price=None)


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0004_drf2984_tariff_extra_trial_frozen"),
    ]

    operations = [
        migrations.RunPython(seed_extra_master_price, unseed_extra_master_price),
    ]
