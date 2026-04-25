"""Tests for DRF-196: ServiceTemplate model + seed migration."""
from __future__ import annotations

import pytest
from django.core.exceptions import ValidationError
from django.db.utils import IntegrityError

from services.models import ServiceCategory, ServiceTemplate


@pytest.mark.django_db
class TestServiceTemplateModel:
    @pytest.fixture
    def category(self) -> ServiceCategory:
        return ServiceCategory.objects.create(name='Test category')

    def test_create_template(self, category):
        tpl = ServiceTemplate.objects.create(
            category=category,
            name='Тестовый шаблон',
            name_short='Тест',
            duration_default=60,
            duration_min=30,
            duration_max=90,
        )
        assert str(tpl) == 'Тестовый шаблон (Test category)'
        assert tpl.is_popular is False
        assert tpl.sort_order == 0

    def test_unique_per_category(self, category):
        ServiceTemplate.objects.create(
            category=category, name='Дубль', name_short='Д',
            duration_default=60, duration_min=30, duration_max=90,
        )
        with pytest.raises(IntegrityError):
            ServiceTemplate.objects.create(
                category=category, name='Дубль', name_short='Д',
                duration_default=60, duration_min=30, duration_max=90,
            )

    def test_duration_min_must_be_le_default(self, category):
        tpl = ServiceTemplate(
            category=category, name='Плохие длительности', name_short='Плох',
            duration_default=60, duration_min=90, duration_max=120,
        )
        with pytest.raises(ValidationError):
            tpl.full_clean()

    def test_duration_max_must_be_ge_default(self, category):
        tpl = ServiceTemplate(
            category=category, name='Плохие длительности 2', name_short='Плох2',
            duration_default=60, duration_min=30, duration_max=45,
        )
        with pytest.raises(ValidationError):
            tpl.full_clean()

    def test_ordering_popular_first(self, category):
        ServiceTemplate.objects.create(
            category=category, name='Б не популярный', name_short='Б',
            duration_default=30, duration_min=15, duration_max=60,
            sort_order=1,
        )
        ServiceTemplate.objects.create(
            category=category, name='А популярный', name_short='А',
            duration_default=30, duration_min=15, duration_max=60,
            is_popular=True, sort_order=99,
        )
        names = list(
            ServiceTemplate.objects
            .filter(category=category)
            .values_list('name', flat=True)
        )
        assert names[0] == 'А популярный'


@pytest.mark.django_db
class TestSeedServiceTemplatesCommand:
    """Проверяет management-команду `seed_service_templates` (DRF-196)."""

    EXPECTED_CATEGORY_SLUGS = {
        'nails', 'manicure', 'pedicure', 'hair',
        'brows-lashes', 'cosmetology', 'massage', 'makeup',
    }
    TEMPLATE_CATEGORIES = {
        'manicure', 'pedicure', 'brows-lashes',
        'massage', 'cosmetology', 'hair', 'makeup',
    }

    @pytest.fixture(autouse=True)
    def run_seed(self):
        from django.core.management import call_command
        call_command('seed_service_templates', verbosity=0)

    def test_categories_seeded(self):
        slugs = set(ServiceCategory.objects.values_list('slug', flat=True))
        assert self.EXPECTED_CATEGORY_SLUGS.issubset(slugs)

    def test_parent_relations(self):
        nails = ServiceCategory.objects.get(slug='nails')
        manicure = ServiceCategory.objects.get(slug='manicure')
        pedicure = ServiceCategory.objects.get(slug='pedicure')
        assert manicure.parent_id == nails.pk
        assert pedicure.parent_id == nails.pk

    def test_at_least_40_templates_across_7_categories(self):
        total = ServiceTemplate.objects.count()
        assert total >= 40
        categories_with_templates = set(
            ServiceTemplate.objects
            .values_list('category__slug', flat=True)
            .distinct()
        )
        assert self.TEMPLATE_CATEGORIES.issubset(categories_with_templates)

    def test_top3_popular_per_category(self):
        for slug in self.TEMPLATE_CATEGORIES:
            popular = ServiceTemplate.objects.filter(
                category__slug=slug, is_popular=True,
            ).count()
            assert popular == 3, f"{slug}: ожидалось 3 популярных, получено {popular}"

    def test_duration_defaults_within_bounds(self):
        for tpl in ServiceTemplate.objects.all():
            assert tpl.duration_min <= tpl.duration_default <= tpl.duration_max, (
                f"{tpl.name}: duration_min={tpl.duration_min}, "
                f"duration_default={tpl.duration_default}, "
                f"duration_max={tpl.duration_max}"
            )

    def test_idempotent(self):
        from django.core.management import call_command
        count_before = ServiceTemplate.objects.count()
        call_command('seed_service_templates', verbosity=0)
        call_command('seed_service_templates', verbosity=0)
        assert ServiceTemplate.objects.count() == count_before
