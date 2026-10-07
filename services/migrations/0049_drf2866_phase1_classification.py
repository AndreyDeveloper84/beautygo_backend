"""Фаза 1 пакета service_family: область по разделам и класс шести канонам (DRF-2866).

Решение владельца 07.10. Схема не меняется — это данные: правило, отбор и
условия отказа — в ``_drf2866_phase1_classification``, там же они
проверяются тестом. Поведение читателей остаётся под флагом
``BODY_CARE_UNCLASSIFIED_FAIL_CLOSED``: сама миграция ничего не закрывает и
не открывает.
"""

from __future__ import annotations

from django.db import migrations
from django.utils import timezone

from users.migrations import _owner_provenance_account as owner_account

from . import _drf2866_phase1_classification as phase1


def classify(apps, schema_editor):
    counts = phase1.classify(
        apps.get_model("services", "ServiceTemplate"),
        owner_id=owner_account.account_id(apps.get_model("users", "User")),
        now=timezone.now(),
    )
    print(
        f"  0049 drf2866: класс поставлен {counts['classed']}; "
        f"область — владельцем {counts['scoped_by_owner']}, правилом раздела {counts['scoped_by_rule']}; "
        f"с неизвестной областью осталось {counts['left_unknown']}"
    )


def declassify(apps, schema_editor):
    counts = phase1.declassify(apps.get_model("services", "ServiceTemplate"))
    print(f"  0049 drf2866: класс снят у {counts['declassed']}, область снята у {counts['unscoped']}")


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0048_body_care_scope"),
        ("users", "0031_owner_provenance_account"),
    ]

    operations = [
        migrations.RunPython(classify, declassify),
    ]
