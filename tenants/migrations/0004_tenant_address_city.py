"""DRF-1587 — адрес и город принадлежат салону (решение владельца 08.09.2026).

До этой миграции ``tenants.Tenant`` нёс ровно пять полей
(``id``/``slug``/``name``/``is_active``/``created_at``/``updated_at``):
ни адреса, ни города, ни координат. Адрес жил только на
``users.SpecialistProfile.address`` и дублировался у каждого мастера
салона одной и той же строкой; город в Ayla не жил вовсе.

Миграция ЧИСТО схемная: ни одной строки не заполняется.

Почему нет backfill из ``SpecialistProfile.address``: он был бы догадкой.
Правило старшинства «салон против мастера» — отдельная задача DRF-1589,
и до её решения перенос адреса мастера в салон означал бы, что мы уже
ответили на её вопрос. Пустое поле здесь честно значит «не указано».

Почему нет координат: ``location_lat``/``location_lng`` пусты у всех 31
мастера пилота — это факт, а не пробел. Нужны ли они салону и откуда их
брать — открытый вопрос владельцу.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tenants", "0003_seed_default_tenants"),
    ]

    operations = [
        migrations.AddField(
            model_name="tenant",
            name="address",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Адрес салона (или мастера-одиночки), как его показывают "
                    "клиенту. Пусто = не указан; наружу уедет null, а не "
                    "пустая строка."
                ),
                max_length=500,
            ),
        ),
        migrations.AddField(
            model_name="tenant",
            name="city",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Город салона. Пусто = не указан; такой салон не попадает "
                    "ни в один городской ответ поиска. Наружу уедет null, а "
                    "не пустая строка."
                ),
                max_length=120,
            ),
        ),
    ]
