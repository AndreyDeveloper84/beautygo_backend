"""DRF-1624 — состояние догоняет модель: из `Status` убран `in_progress`.

`e2b218b` (DRF-1120 + DRF-1115, AndreyDeveloper84, 20.08.2026, PR #230)
снял `Appointment.Status.IN_PROGRESS` из перечисления, но миграцию на
изменившийся `choices` не выпустил. С тех пор `makemigrations` просит
эту `AlterField` на каждом прогоне.

Почему статус убрали, чтобы через полгода не искали: `IN_PROGRESS` был
объявлен в перечислении, но его не было в `_BOOKING_TRANSITIONS`.
Поставить его могла только ручная правка в админке, а выйти из него
было нельзя ничем: `Appointment.booking_status` делает
`BookingStatus(self.status)` и падает на нём `ValueError`, а
`can_cancel` / `can_complete` / `can_mark_no_show` этот `ValueError`
глотают и отвечают «нельзя». Один клик в админке делал бронь навсегда
неотменяемой и незавершаемой — и, поскольку `is_active` глотает тот же
`ValueError`, освобождал её слот под двойную запись. Статус сняли, а не
дописали в переходы: решения владельца о настоящем статусе «в процессе»
не было (архитектурное ревью 2026-08-15, §6, §7).

SQL эта миграция не порождает. `choices` живут только в Django: на
PostgreSQL поле остаётся `varchar(20)`, никакого `CHECK` по списку
значений Django для `choices` не выпускает. Проверено — `sqlmigrate
appointments 0018` печатает `-- (no-op)`. Так что миграция приводит в
порядок запись Django о поле, а не само поле.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("appointments", "0017_drf1064_closure_attribution"),
    ]

    operations = [
        migrations.AlterField(
            model_name="appointment",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Ожидает подтверждения"),
                    ("awaiting_payment", "Ожидает оплаты"),
                    ("confirmed", "Подтверждена"),
                    ("completed", "Завершена"),
                    ("cancelled", "Отменена"),
                    ("no_show", "Клиент не пришёл"),
                ],
                db_index=True,
                default="pending",
                max_length=20,
            ),
        ),
    ]
