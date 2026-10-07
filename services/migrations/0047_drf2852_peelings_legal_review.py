"""Четыре канона-пилинга салона пилота — на юридическую проверку (DRF-2852).

Решение владельца 07.10. Схема не меняется — это данные: правило, отбор и
условия отказа — в ``_drf2852_peelings_legal_review``, там же они проверяются
тестом.
"""

from __future__ import annotations

from django.conf import settings
from django.db import migrations
from django.utils import timezone

from . import _drf2852_peelings_legal_review as peelings


def close_peelings(apps, schema_editor):
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    counts = peelings.close(
        apps.get_model("services", "ServiceTemplate"),
        apps.get_model(app_label, model_name),
        confirmed_by_id=peelings.CONFIRMED_BY_ID,
        now=timezone.now(),
    )
    print(
        f"  0047 drf2852: канонов найдено {counts['found']}, "
        f"закрыто {counts['closed']}, уже с классом {counts['already_classed']}"
    )


def reopen_peelings(apps, schema_editor):
    reopened = peelings.reopen(apps.get_model("services", "ServiceTemplate"))
    print(f"  0047 drf2852: класс снят у {reopened}")


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0046_body_care_7a0_legal_class"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(close_peelings, reopen_peelings),
    ]
