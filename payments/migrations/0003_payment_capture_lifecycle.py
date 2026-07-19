# Two-stage capture lifecycle fields (AYLA-DEC-0009/D9 + C3 payout preview).
#
# NOTE: ``makemigrations`` also detects PRE-EXISTING drift unrelated to
# this migration — ``AlterModelTable(table=None)`` (state cleanup after
# 0002's explicit rename, #492) and ``AlterField(amount)`` (validator
# state). Both are SQL no-ops and intentionally NOT included here;
# reported to the orchestrator for the owning stream.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0002_rename_table"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="capture_state",
            field=models.CharField(
                choices=[
                    ("", "Не применимо"),
                    ("scheduled", "Холд есть, capture запланирован"),
                    (
                        "captured_pending_settlement",
                        "Capture выполнен, ждёт выплаты ЮKassa",
                    ),
                    ("settled", "Выплачено мастеру"),
                    ("capture_failed", "Capture не удался"),
                    ("canceled", "Холд отменён"),
                    ("refunded", "Возвращено клиенту"),
                ],
                db_index=True,
                default="",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="capture_scheduled_for",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="yookassa_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="captured_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
