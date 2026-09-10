"""DRF-1624 — состояние догоняет модель: валидатор суммы и имя таблицы.

Два расхождения, оба чисто в состоянии, оба без SQL. Их замечали и
обходили в `0003_payment_capture_lifecycle` и `0004_userpaymentmethod`
(«PRE-EXISTING drift … intentionally NOT included here»); эта миграция
их закрывает.

1. `amount`. Модель объявляет
   `validators=[MinValueValidator(Decimal("0.01"))]`, а
   `0001_initial` записал `MinValueValidator(0.01)` — с float'ом.
   `BaseValidator.__eq__` сравнивает `limit_value`, а
   `Decimal("0.01") != 0.01`, поэтому поля не совпадают. Валидаторы
   работают в Python на `full_clean()` и в DRF; в схеме их нет вовсе,
   так что база об этой разнице никогда не знала.

2. `db_table`. `0002_rename_table` выполнил переезд
   `appointments_payment` → `payments_payment` через
   `AlterModelTable(table="payments_payment")`, то есть записал имя
   ЯВНО. Модель же никакого `db_table` не объявляет и полагается на
   умолчание Django `<app_label>_<modelname>`, которое даёт РОВНО ту же
   строку — `payments_payment`. Автодетектор сравнивает «явно задано»
   с «не задано» и просит `AlterModelTable(table=None)`.

   Имя таблицы при этом НЕ МЕНЯЕТСЯ. `table=None` означает «взять
   умолчание», а умолчание здесь и есть `payments_payment` — то самое
   имя, что стоит в базе с #492. Django это видит:
   `BaseDatabaseSchemaEditor.alter_db_table` выходит сразу, когда
   старое и новое имена совпадают, поэтому `ALTER TABLE … RENAME TO …`
   не выпускается. Проверено — `sqlmigrate payments 0005` печатает
   `-- (no-op)` на ОБЕ операции.

Менять модель под `0001` (вернуть float) или дописывать в неё
`db_table` было бы шагом назад: `payments/models.py` в докстринге прямо
просит не заводить `db_table` (Django E028 ловит дубли имён между
моделями). Поэтому в состояние приезжает модель, а не наоборот.
"""

import django.core.validators
from decimal import Decimal
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("payments", "0004_userpaymentmethod"),
    ]

    operations = [
        migrations.AlterField(
            model_name="payment",
            name="amount",
            field=models.DecimalField(
                decimal_places=2,
                max_digits=10,
                validators=[django.core.validators.MinValueValidator(Decimal("0.01"))],
            ),
        ),
        migrations.AlterModelTable(
            name="payment",
            table=None,
        ),
    ]
