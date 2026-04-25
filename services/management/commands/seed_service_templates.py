"""Seed ServiceCategory + ≥40 ServiceTemplate records (DRF-196).

Идемпотентная команда: повторный прогон не создаёт дубликатов и не ломает
существующие данные, что позволяет безопасно запускать её и на dev, и на
проде после деплоя.

Usage:
    python manage.py seed_service_templates
"""
from __future__ import annotations

from django.core.management.base import BaseCommand
from django.db import transaction

from services.models import ServiceCategory, ServiceTemplate

# (slug, name, icon, sort_order, parent_slug | None)
CATEGORIES: list[tuple[str, str, str, int, str | None]] = [
    ('nails', 'Ногтевой сервис', 'nail-polish', 10, None),
    ('manicure', 'Маникюр', 'nail-polish', 10, 'nails'),
    ('pedicure', 'Педикюр', 'nail-polish', 20, 'nails'),
    ('hair', 'Волосы', 'scissors', 20, None),
    ('brows-lashes', 'Брови и ресницы', 'eye', 30, None),
    ('cosmetology', 'Косметология', 'sparkles', 40, None),
    ('massage', 'Массаж', 'hand', 50, None),
    ('makeup', 'Макияж', 'brush', 60, None),
]

# (category_slug, name, name_short, duration_default, duration_min, duration_max, is_popular, sort_order)
TEMPLATES: list[tuple[str, str, str, int, int, int, bool, int]] = [
    # --- Маникюр (10) ---
    ('manicure', 'Классический маникюр', 'Классика', 60, 30, 90, True, 10),
    ('manicure', 'Маникюр + покрытие гель-лаком', 'Гель-лак', 90, 60, 120, True, 20),
    ('manicure', 'Аппаратный маникюр', 'Аппаратный', 60, 30, 90, True, 30),
    ('manicure', 'Наращивание ногтей (гель)', 'Гель', 120, 90, 180, False, 40),
    ('manicure', 'Наращивание ногтей (акрил)', 'Акрил', 120, 90, 180, False, 50),
    ('manicure', 'Дизайн ногтей (1 палец)', 'Дизайн 1', 15, 10, 30, False, 60),
    ('manicure', 'Дизайн ногтей (все пальцы)', 'Дизайн', 30, 20, 60, False, 70),
    ('manicure', 'Снятие гель-лака', 'Снятие', 20, 15, 40, False, 80),
    ('manicure', 'Снятие нарощенных ногтей', 'Снятие ногтей', 40, 30, 60, False, 90),
    ('manicure', 'Укрепление ногтей (биогель)', 'Биогель', 60, 40, 90, False, 100),

    # --- Педикюр (6) ---
    ('pedicure', 'Классический педикюр', 'Классика', 60, 40, 90, True, 10),
    ('pedicure', 'Аппаратный педикюр', 'Аппаратный', 60, 40, 90, True, 20),
    ('pedicure', 'Педикюр + покрытие гель-лаком', 'Гель-лак', 90, 60, 120, True, 30),
    ('pedicure', 'Spa-педикюр', 'Spa', 90, 60, 120, False, 40),
    ('pedicure', 'Наращивание ногтей на ногах', 'Наращивание', 120, 90, 180, False, 50),
    ('pedicure', 'Снятие гель-лака (ноги)', 'Снятие', 20, 15, 40, False, 60),

    # --- Брови и ресницы (8) ---
    ('brows-lashes', 'Коррекция бровей воском', 'Коррекция', 20, 15, 40, True, 10),
    ('brows-lashes', 'Окрашивание бровей', 'Окрашивание', 30, 20, 60, True, 20),
    ('brows-lashes', 'Ламинирование бровей', 'Ламинирование', 60, 45, 90, True, 30),
    ('brows-lashes', 'Микроблейдинг', 'Микроблейдинг', 120, 90, 180, False, 40),
    ('brows-lashes', 'Наращивание ресниц (классика)', 'Классика', 120, 90, 180, False, 50),
    ('brows-lashes', 'Наращивание ресниц (объём)', 'Объём', 150, 120, 210, False, 60),
    ('brows-lashes', 'Ламинирование ресниц', 'Ламинирование ресниц', 60, 45, 90, False, 70),
    ('brows-lashes', 'Снятие ресниц', 'Снятие', 30, 20, 45, False, 80),

    # --- Массаж (6) ---
    ('massage', 'Массаж спины', 'Спина', 30, 20, 45, True, 10),
    ('massage', 'Массаж тела (60 мин)', 'Тело 60', 60, 45, 90, True, 20),
    ('massage', 'Массаж тела (90 мин)', 'Тело 90', 90, 75, 120, True, 30),
    ('massage', 'Антицеллюлитный массаж', 'Антицеллюлит', 60, 45, 90, False, 40),
    ('massage', 'Массаж лица', 'Лицо', 40, 30, 60, False, 50),
    ('massage', 'Расслабляющий массаж', 'Расслабляющий', 60, 45, 90, False, 60),

    # --- Косметология (6) ---
    ('cosmetology', 'Чистка лица (мануальная)', 'Чистка мануал.', 60, 45, 90, True, 10),
    ('cosmetology', 'Чистка лица (ультразвуковая)', 'Чистка УЗ', 45, 30, 60, True, 20),
    ('cosmetology', 'Пилинг лица', 'Пилинг', 30, 20, 60, True, 30),
    ('cosmetology', 'Уходовая процедура', 'Уход', 60, 45, 90, False, 40),
    ('cosmetology', 'Мезотерапия', 'Мезотерапия', 60, 45, 90, False, 50),
    ('cosmetology', 'RF-лифтинг', 'RF-лифтинг', 45, 30, 60, False, 60),

    # --- Волосы (6) ---
    ('hair', 'Стрижка женская', 'Стрижка жен.', 60, 40, 90, True, 10),
    ('hair', 'Стрижка мужская', 'Стрижка муж.', 30, 20, 45, True, 20),
    ('hair', 'Окрашивание (1 цвет)', 'Окрашивание', 120, 90, 150, True, 30),
    ('hair', 'Окрашивание (сложное)', 'Сложное', 180, 150, 240, False, 40),
    ('hair', 'Укладка', 'Укладка', 40, 30, 60, False, 50),
    ('hair', 'Кератиновое выпрямление', 'Кератин', 180, 150, 240, False, 60),

    # --- Макияж (4) ---
    ('makeup', 'Дневной макияж', 'Дневной', 60, 45, 90, True, 10),
    ('makeup', 'Вечерний макияж', 'Вечерний', 75, 60, 90, True, 20),
    ('makeup', 'Свадебный макияж', 'Свадебный', 90, 75, 120, True, 30),
    ('makeup', 'Обучение макияжу', 'Обучение', 120, 90, 180, False, 40),
]


class Command(BaseCommand):
    help = "Seed ServiceCategory + ServiceTemplate reference data (DRF-196)."

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        created_categories, created_templates = self._seed()
        self.stdout.write(self.style.SUCCESS(
            f"Seed complete: +{created_categories} categories, "
            f"+{created_templates} templates "
            f"(total categories={ServiceCategory.objects.count()}, "
            f"templates={ServiceTemplate.objects.count()})"
        ))

    @staticmethod
    def _seed() -> tuple[int, int]:
        created_categories = 0
        # Roots first.
        for slug, name, icon, sort_order, parent_slug in CATEGORIES:
            if parent_slug is not None:
                continue
            _, created = ServiceCategory.objects.get_or_create(
                slug=slug,
                defaults={
                    'name': name,
                    'icon': icon,
                    'sort_order': sort_order,
                    'is_active': True,
                },
            )
            created_categories += int(created)

        roots = {
            c.slug: c
            for c in ServiceCategory.objects.filter(parent__isnull=True)
        }
        for slug, name, icon, sort_order, parent_slug in CATEGORIES:
            if parent_slug is None:
                continue
            parent = roots.get(parent_slug)
            if parent is None:
                continue
            _, created = ServiceCategory.objects.get_or_create(
                slug=slug,
                defaults={
                    'name': name,
                    'icon': icon,
                    'sort_order': sort_order,
                    'is_active': True,
                    'parent': parent,
                },
            )
            created_categories += int(created)

        categories_by_slug = {
            c.slug: c for c in ServiceCategory.objects.all()
        }
        created_templates = 0
        for (
            slug, name, name_short,
            duration_default, duration_min, duration_max,
            is_popular, sort_order,
        ) in TEMPLATES:
            category = categories_by_slug.get(slug)
            if category is None:
                continue
            _, created = ServiceTemplate.objects.update_or_create(
                category=category,
                name=name,
                defaults={
                    'name_short': name_short,
                    'duration_default': duration_default,
                    'duration_min': duration_min,
                    'duration_max': duration_max,
                    'is_popular': is_popular,
                    'sort_order': sort_order,
                },
            )
            created_templates += int(created)

        return created_categories, created_templates
