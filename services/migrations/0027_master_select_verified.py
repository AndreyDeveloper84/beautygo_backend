"""Разовый прогон правила по уже существующим строкам выбора мастера (DRF-2406).

Решение владельца §77 п.30 (24.09): выбор мастера из канона — достаточное
основание, связь подтверждается **правилом**. Живой путь закрыт в
``services/offer_selection.py``; здесь закрываются строки, выбранные **до**
этой правки.

Признак отбора назван явно и узко: ``mapping_source_ref`` начинается с
``master_select:``, статус ``review_required``, привязка к канону есть.

**Почему не «все review_required».** На стенде 24.09 их 212: шесть — выбор
мастера, 206 — сид, и у строк сида ``mapping_source_ref`` **пуст**. Подтвердить
их этим правилом значило бы написать «мастер выбрал» там, где мастер не
выбирал, — подлог провенанса. Провенанс потом читают как факт, поэтому такая
ошибка хуже незакрытой очереди. Строки сида закрываются **своим** правилом со
своей версией — DRF-2408.

**Почему привязка обязательна.** ``VERIFIED`` без шаблона запрещён схемой
(``salonservice_verified_requires_template``): подтверждать нечего, если связи
нет. Строка без привязки правилом не подтверждается — это узел листа, а не
следствие реализации.
"""

from __future__ import annotations

from django.db import migrations
from django.utils import timezone

#: Литералы намеренно продублированы из ``services.offer_selection``: миграция
#: не вправе зависеть от кода приложения, который завтра переименуют. Расхождение
#: устранить нельзя — поэтому оно сторожится узлом
#: ``test_migration_rule_matches_the_live_rule``.
RULE = "master_selected_from_canon"
RULE_VERSION = "1"
SOURCE_REF_PREFIX = "master_select:"


def confirm_master_selections(apps, schema_editor):
    SalonService = apps.get_model("services", "SalonService")
    rows = SalonService.objects.filter(
        mapping_source_ref__startswith=SOURCE_REF_PREFIX,
        mapping_status="review_required",
        template__isnull=False,
    )
    # Дата у всех одна: это один прогон одного правила, и по ней потом видно,
    # что строки подтверждены разом, а не поодиночке кем-то вручную.
    touched = rows.update(
        mapping_status="verified",
        mapping_confirmed_rule=RULE,
        mapping_rule_version=RULE_VERSION,
        mapping_confirmed_at=timezone.now(),
    )
    skipped = SalonService.objects.filter(
        mapping_source_ref__startswith=SOURCE_REF_PREFIX,
        mapping_status="review_required",
        template__isnull=True,
    ).count()
    print(
        f"  0027 master_select: подтверждено правилом {touched}; "
        f"без привязки к канону пропущено {skipped}"
    )


def unconfirm(apps, schema_editor):
    """Обратно — только строки, подтверждённые именно этим правилом.

    По имени правила, а не по всем ``verified``: откат не вправе снимать
    подтверждение, поставленное человеком или другим правилом.

    Это **снятие миграции**, а не путь в продукте. В продукте отката на
    ``review_required`` нет и не должно быть: он запрещён формулировкой
    владельца в докстринге ``MappingStatus``, потому что снова сделал бы
    очередь вечной.
    """

    SalonService = apps.get_model("services", "SalonService")
    SalonService.objects.filter(mapping_confirmed_rule=RULE).update(
        mapping_status="review_required",
        mapping_confirmed_rule="",
        mapping_rule_version="",
        mapping_confirmed_at=None,
    )


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0026_goaldirection"),
    ]

    operations = [
        migrations.RunPython(confirm_master_selections, unconfirm),
    ]
