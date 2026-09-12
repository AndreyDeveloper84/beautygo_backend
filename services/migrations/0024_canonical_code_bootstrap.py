"""Bootstrap ``ServiceTemplate.canonical_code`` из seed — fail-closed (MAP-AUTO-02 (a)).

Ключ — пара ``(category.name, name)``, ровно та, которой ``seed_canonical_catalog``
кладёт строку в базу; код — из ``services/seeds/canonical_catalog_2026-07.json``.
Ставится только строкам с пустым кодом. Любая неоднозначность (seed не
биекция; в базе стоит другой код; один код на две строки) — отказ **без единой
записи**: миграция падает внутри транзакции и печатает список причин. Это не
тихий backfill.

Пары seed, которых в базе нет, — не отказ: пустая база (CI), тестовая
фикстура и база с чужими 40 шаблонами DRF-196 законны; такие пары считаются
и печатаются числом. Чужие шаблоны остаются ``NULL``.

Обратный ход обнуляет **только** коды из seed, не «все».
Логика — в ``services/canonical_code.py``, общая с тестами.
"""
from django.db import migrations

from services.canonical_code import bootstrap_canonical_codes, unbootstrap_canonical_codes


def forwards(apps, schema_editor):
    ServiceTemplate = apps.get_model("services", "ServiceTemplate")
    report = bootstrap_canonical_codes(ServiceTemplate)   # BootstrapRefused → миграция падает, транзакция откатывается
    for line in report.lines():
        print(f"  0024 canonical_code bootstrap: {line}")


def backwards(apps, schema_editor):
    ServiceTemplate = apps.get_model("services", "ServiceTemplate")
    n = unbootstrap_canonical_codes(ServiceTemplate)
    print(f"  0024 canonical_code bootstrap: снято кодов из seed — {n}")


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0023_servicetemplate_canonical_code"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
