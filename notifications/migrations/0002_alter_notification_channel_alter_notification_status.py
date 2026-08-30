"""DRF-1030 — two new enum values, no data change.

``Channel.MAX`` and ``Status.HANDED_OFF`` widen the vocabulary so a row
can say "bot-platform delivers this one, and Ayla passed it on" instead
of reporting a push failure for a message that was never going to travel
by push. Choices-only ``AlterField`` — PostgreSQL rewrites nothing, the
columns are unchanged CharFields, and every historical row keeps its
value. ``"handed_off"`` is exactly the 10 characters ``max_length``
allows; a longer status would need a column change.

Existing rows are deliberately NOT backfilled. The 45 mislabelled
``failed`` rows on the pilot are a record of what Ayla believed at the
time; rewriting them would erase the evidence for the ticket. New rows
are correct from this migration forward.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="notification",
            name="channel",
            field=models.CharField(
                choices=[
                    ("push", "Push"),
                    ("sms", "SMS"),
                    ("both", "Push + SMS fallback"),
                    ("max", "MAX (доставляет бот)"),
                ],
                max_length=10,
            ),
        ),
        migrations.AlterField(
            model_name="notification",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "В очереди"),
                    ("sent", "Отправлено"),
                    ("failed", "Ошибка"),
                    ("skipped", "Пропущено"),
                    ("handed_off", "Передано боту (доставку владеет бот)"),
                ],
                default="pending",
                max_length=10,
            ),
        ),
    ]
