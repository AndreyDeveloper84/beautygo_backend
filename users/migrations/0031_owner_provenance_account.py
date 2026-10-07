"""Именная учётка владельца для провенанса решений о каноне.

Решение владельца 07.10. Схема не меняется — это данные: что за учётка, как
на неё ссылаться и когда шаг отказывает — в ``_owner_provenance_account``,
там же это проверяется тестом.
"""

from __future__ import annotations

from django.contrib.auth.hashers import make_password
from django.db import migrations

from . import _owner_provenance_account as owner_account


def ensure_account(apps, schema_editor):
    _, created = owner_account.ensure(
        apps.get_model("users", "User"), unusable_password=make_password(None)
    )
    print(f"  0031 owner provenance account: {'заведена' if created else 'уже была'}")


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0030_body_care_7a4_practitioner_qualification"),
    ]

    operations = [
        # Обратного хода нет намеренно: на учётку ссылаются провенансы под PROTECT.
        migrations.RunPython(ensure_account, migrations.RunPython.noop),
    ]
