"""Сторож нерешённого признака демо-салона (DRF-2646).

На пилоте 29.09: у всех 22 тенантов ``is_demo = false``, хотя пятеро заведены
сидом демо-салонов. Признак, который всегда ``false``, хуже отсутствующего:
он ВЫГЛЯДИТ работающим — десять пулов выдачи клиенту (``users.sellable.
demo_scope_q``) исправно спрашивают его и не отсекают ничего, и любой замер,
взвешивающий салоны по ``is_demo``, получает «все настоящие».

Причина — не код пометки: миграция ``tenants 0009`` проставила строкам,
жившим до поля, ``False``, а ``mark_demo_and_test_personas --apply`` после неё
не запускали. Сторож краснеет на НЕРЕШЁННОМ признаке — салон из файла сида,
а ``is_demo = false`` — и называет его по имени. Данных не пишет: пометка
видимая (демо видят только тестовые личности, DRF-2420), и она — решение
владельца.

Проверка с тегом ``database``: Django зовёт её из ``migrate`` с
``databases=[…]`` — то есть на каждой выкладке, по живым данным; обычный
``manage.py check`` (CI без базы) передаёт ``databases=None``, и сторож молчит.
"""

from __future__ import annotations

from django.core.checks import Tags, Warning, register
from django.db import DatabaseError

from tenants.demo_seed import DemoSeedUnreadable, demo_seed_slugs


@register(Tags.database)
def seeded_demo_salons_are_marked(app_configs, databases=None, **kwargs):
    if not databases:
        return []
    try:
        slugs = demo_seed_slugs()
    except DemoSeedUnreadable as exc:
        return [Warning(f"Сторож демо-салонов не может прочесть сид: {exc}", id="tenants.W002")]

    from tenants.models import Tenant

    try:
        unmarked = sorted(
            Tenant.all_objects.filter(slug__in=slugs, is_demo=False).values_list("slug", flat=True)
        )
    except DatabaseError:
        # Базы ещё нет (первый migrate) или таблица без поля — не повод ронять выкладку.
        return []
    if not unmarked:
        return []
    return [
        Warning(
            "Салоны из файла сида демо не помечены демонстрационными (Tenant.is_demo = false): "
            f"{', '.join(unmarked)}. Клиенту их не прячет ни один пул выдачи (users.sellable).",
            hint=(
                "Пометка видимая: демо видят только тестовые личности (DRF-2420). Сперва "
                "`manage.py mark_demo_and_test_personas --persona <кто тестирует>`, затем "
                "тот же вызов с --apply (DRF-2646)."
            ),
            id="tenants.W001",
        )
    ]
