"""Seed рекомендованных цен для `penza` + `default` (DRF-197).

Идемпотентная команда: повторный прогон обновляет существующие цены через
`update_or_create` и не создаёт дубликатов. Запускать после
`seed_service_templates` (или автоматически — если шаблон не найден,
соответствующая строка просто пропускается).

Usage:
    python manage.py seed_regional_pricing
"""
from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from services.models import RegionalPricing, ServiceTemplate

REGIONS: dict[str, str] = {
    'penza': 'Пенза',
    'default': 'По умолчанию',
}

# Формат: template_name → {region_key: (min, max)}
PRICING: dict[str, dict[str, tuple[int, int]]] = {
    # --- Маникюр ---
    'Классический маникюр':          {'penza': (600, 1000),  'default': (1000, 2000)},
    'Маникюр + покрытие гель-лаком': {'penza': (1000, 1600), 'default': (1500, 3000)},
    'Аппаратный маникюр':            {'penza': (800, 1200),  'default': (1200, 2500)},
    'Наращивание ногтей (гель)':     {'penza': (1200, 2000), 'default': (2000, 4000)},
    'Наращивание ногтей (акрил)':    {'penza': (1200, 2000), 'default': (2000, 4000)},
    'Дизайн ногтей (1 палец)':       {'penza': (100, 200),   'default': (200, 400)},
    'Дизайн ногтей (все пальцы)':    {'penza': (300, 600),   'default': (600, 1200)},
    'Снятие гель-лака':              {'penza': (200, 400),   'default': (300, 700)},
    'Снятие нарощенных ногтей':      {'penza': (400, 700),   'default': (700, 1400)},
    'Укрепление ногтей (биогель)':   {'penza': (700, 1200),  'default': (1200, 2500)},

    # --- Педикюр ---
    'Классический педикюр':          {'penza': (800, 1200),  'default': (1200, 2500)},
    'Аппаратный педикюр':            {'penza': (1000, 1500), 'default': (1500, 3000)},
    'Педикюр + покрытие гель-лаком': {'penza': (1200, 2000), 'default': (2000, 4000)},
    'Spa-педикюр':                   {'penza': (1500, 2200), 'default': (2200, 4500)},
    'Наращивание ногтей на ногах':   {'penza': (1500, 2500), 'default': (2500, 5000)},
    'Снятие гель-лака (ноги)':       {'penza': (250, 500),   'default': (400, 900)},

    # --- Брови и ресницы ---
    'Коррекция бровей воском':       {'penza': (300, 600),   'default': (500, 1200)},
    'Окрашивание бровей':            {'penza': (400, 800),   'default': (700, 1500)},
    'Ламинирование бровей':          {'penza': (1200, 2000), 'default': (2000, 4000)},
    'Микроблейдинг':                 {'penza': (4000, 7000), 'default': (7000, 15000)},
    'Наращивание ресниц (классика)': {'penza': (1500, 2500), 'default': (2500, 5000)},
    'Наращивание ресниц (объём)':    {'penza': (2000, 3500), 'default': (3500, 6000)},
    'Ламинирование ресниц':          {'penza': (1500, 2500), 'default': (2500, 5000)},
    'Снятие ресниц':                 {'penza': (300, 600),   'default': (500, 1000)},

    # --- Массаж ---
    'Массаж спины':            {'penza': (600, 1000),  'default': (1000, 2500)},
    'Массаж тела (60 мин)':    {'penza': (1000, 1800), 'default': (1800, 4000)},
    'Массаж тела (90 мин)':    {'penza': (1500, 2500), 'default': (2500, 5000)},
    'Антицеллюлитный массаж':  {'penza': (1200, 2000), 'default': (2000, 4500)},
    'Массаж лица':             {'penza': (800, 1400),  'default': (1400, 3000)},
    'Расслабляющий массаж':    {'penza': (1000, 1800), 'default': (1800, 3500)},

    # --- Косметология ---
    'Чистка лица (мануальная)':      {'penza': (1500, 2500), 'default': (2500, 5000)},
    'Чистка лица (ультразвуковая)':  {'penza': (1200, 2000), 'default': (2000, 4000)},
    'Пилинг лица':                   {'penza': (800, 1500),  'default': (1500, 3500)},
    'Уходовая процедура':            {'penza': (1500, 2500), 'default': (2500, 5000)},
    'Мезотерапия':                   {'penza': (2500, 4000), 'default': (4000, 8000)},
    'RF-лифтинг':                    {'penza': (1500, 2500), 'default': (2500, 5000)},

    # --- Волосы ---
    'Стрижка женская':         {'penza': (500, 1000),  'default': (1000, 2500)},
    'Стрижка мужская':         {'penza': (300, 600),   'default': (600, 1500)},
    'Окрашивание (1 цвет)':    {'penza': (1500, 2500), 'default': (2500, 5000)},
    'Окрашивание (сложное)':   {'penza': (3000, 5000), 'default': (5000, 12000)},
    'Укладка':                 {'penza': (400, 800),   'default': (800, 2000)},
    'Кератиновое выпрямление': {'penza': (3500, 6000), 'default': (6000, 15000)},

    # --- Макияж ---
    'Дневной макияж':   {'penza': (1000, 1500), 'default': (1500, 3500)},
    'Вечерний макияж':  {'penza': (1500, 2500), 'default': (2500, 5000)},
    'Свадебный макияж': {'penza': (3000, 5000), 'default': (5000, 10000)},
    'Обучение макияжу': {'penza': (2000, 4000), 'default': (4000, 10000)},
}


class Command(BaseCommand):
    help = "Seed RegionalPricing reference data for penza + default (DRF-197)."

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        created, updated, missing = self._seed()
        self.stdout.write(self.style.SUCCESS(
            f"Regional pricing seeded: +{created} new, ~{updated} updated, "
            f"{missing} templates missing. "
            f"(total rows={RegionalPricing.objects.count()})"
        ))

    @staticmethod
    def _seed() -> tuple[int, int, int]:
        templates_by_name = {t.name: t for t in ServiceTemplate.objects.all()}
        created = updated = missing = 0

        for template_name, per_region in PRICING.items():
            template = templates_by_name.get(template_name)
            if template is None:
                missing += 1
                continue
            for region_key, (price_min, price_max) in per_region.items():
                _, was_created = RegionalPricing.objects.update_or_create(
                    template=template,
                    region_key=region_key,
                    defaults={
                        'region_name': REGIONS[region_key],
                        'price_min': Decimal(price_min),
                        'price_max': Decimal(price_max),
                    },
                )
                if was_created:
                    created += 1
                else:
                    updated += 1

        return created, updated, missing
