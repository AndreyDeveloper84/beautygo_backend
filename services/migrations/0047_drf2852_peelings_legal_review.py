"""Четыре канона-пилинга салона пилота — на юридическую проверку (DRF-2852).

Решение владельца 07.10. Схема не меняется — это данные: правило, отбор и
условия отказа — в ``_drf2852_peelings_legal_review``, там же они проверяются
тестом.
"""

from __future__ import annotations

from django.contrib.auth.hashers import make_password
from django.db import migrations
from django.utils import timezone

from users.migrations import _owner_provenance_account as owner_account

from . import _drf2852_peelings_legal_review as peelings


def close_peelings(apps, schema_editor):
    user_model = apps.get_model("users", "User")
    made: list[bool] = []

    def author():
        pk, created = owner_account.ensure(user_model, unusable_password=make_password(None))
        made.append(created)
        return pk

    counts = peelings.close(
        apps.get_model("services", "ServiceTemplate"), user_model, author=author, now=timezone.now()
    )
    account = "не понадобилась" if not made else ("заведена" if made[0] else "уже была")
    print(
        f"  0047 drf2852: канонов найдено {counts['found']}, "
        f"закрыто {counts['closed']}, уже с классом {counts['already_classed']}; "
        f"учётка владельца {account}"
    )


def reopen_peelings(apps, schema_editor):
    reopened = peelings.reopen(apps.get_model("services", "ServiceTemplate"))
    print(f"  0047 drf2852: класс снят у {reopened}")


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0046_body_care_7a0_legal_class"),
        ("users", "0030_body_care_7a4_practitioner_qualification"),
    ]

    operations = [
        migrations.RunPython(close_peelings, reopen_peelings),
    ]
