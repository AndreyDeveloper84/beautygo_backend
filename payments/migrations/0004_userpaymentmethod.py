# C7.2 — UserPaymentMethod (saved client cards, consent boundary).
#
# NOTE: ``makemigrations`` keeps detecting the PRE-EXISTING drift
# reported with 0003 (AlterModelTable/AlterField amount — SQL no-ops
# from #492); intentionally NOT included here either.

import django.core.validators
import django.db.models.deletion
import uuid
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0003_payment_capture_lifecycle"),
        ("users", "0014_specialistprofile_yookassa_account_id"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserPaymentMethod",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "payment_method_id",
                    models.CharField(max_length=200, unique=True),
                ),
                ("last4", models.CharField(max_length=4)),
                ("brand", models.CharField(max_length=32)),
                ("consent_version", models.CharField(max_length=64)),
                ("consented_at", models.DateTimeField()),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="payment_methods",
                        to="users.user",
                    ),
                ),
            ],
            options={
                "verbose_name": "Способ оплаты пользователя",
                "verbose_name_plural": "Способы оплаты пользователей",
                "indexes": [
                    models.Index(fields=["user"], name="upm_user_idx"),
                ],
            },
        ),
    ]
