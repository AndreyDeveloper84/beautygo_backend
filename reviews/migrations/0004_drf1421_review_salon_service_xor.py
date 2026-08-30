"""DRF-1421 — отзыв получает ссылку на канонический слой каталога.

Что делает
----------
1. ``AddColumn reviews_review.salon_service_id`` — обнуляемый, без
   DEFAULT. В PostgreSQL 11+ это правка каталога, таблица не
   переписывается.
2. ``ALTER COLUMN service_id DROP NOT NULL`` — тоже только каталог.
3. ``CHECK review_exactly_one_service_source`` — ровно одна из двух
   ссылок. Близнец ``appointment_exactly_one_service_source``.

Что будет с существующими строками
----------------------------------
Ничего: миграция меняет схему, а не данные. Строка старой формы
(``service`` заполнен, ``salon_service`` NULL) удовлетворяет CHECK как
есть, поэтому переносить нечего даже там, где отзывы уже накоплены.

На пилоте их и нет. Ноль здесь не совпадение, а следствие схемы:
``Review.service`` был NOT NULL и ссылался на ``Service``, а в
``Service`` ноль строк — значит и отзывов не могло появиться ни одного
(замер 2026-08-30, см. ``services/catalog_reads.py``).

Сколько идёт
------------
На боевой базе — доли секунды. Обе правки колонок метаданные не
переписывают; стоимость несёт только ``ADD CONSTRAINT``, которое берёт
ACCESS EXCLUSIVE и сканирует таблицу целиком — на нуле строк скан
пустой. Индекс под новый FK строится по пустой колонке.

Обратима
--------
Да, ``migrate reviews 0003`` разворачивает все три операции. С одной
оговоркой: обратный ``AlterField`` возвращает ``service`` в NOT NULL и
упадёт, если к тому моменту сохранён хоть один салонный отзыв
(``service IS NULL``). Пока такой строки нет — откат чистый; после
первого отзыва пилота откат потребует сначала убрать эти строки. Это
свойство самого переезда, а не оформления миграции.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("reviews", "0003_review_review_tenant_created_idx"),
        # ``SalonService`` заводится здесь — без этой зависимости AddField
        # может встать раньше своей цели.
        ("services", "0012_catalog_domain_s3a"),
    ]

    operations = [
        migrations.AddField(
            model_name="review",
            name="salon_service",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="reviews",
                to="services.salonservice",
            ),
        ),
        migrations.AlterField(
            model_name="review",
            name="service",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="reviews",
                to="services.service",
            ),
        ),
        migrations.AddConstraint(
            model_name="review",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("salon_service__isnull", True),
                        ("service__isnull", False),
                    ),
                    models.Q(
                        ("salon_service__isnull", False),
                        ("service__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="review_exactly_one_service_source",
            ),
        ),
    ]
