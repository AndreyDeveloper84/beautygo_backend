import logging
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from services.models import Service, ServiceCategory

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestServiceModel:
    def test_create_service(self, specialist_user):
        service = Service.objects.create(
            specialist=specialist_user.specialist_profile,
            name='Стрижка',
            price=Decimal('500.00'),
            duration_minutes=60,
        )
        logger.info("Created service: %s", service)
        assert str(service) == 'Стрижка — Елена Мастер'
        assert service.created_at is not None

    def test_cascade_delete(self, specialist_user):
        Service.objects.create(
            specialist=specialist_user.specialist_profile, name='Test',
            price=Decimal('100'), duration_minutes=30,
        )
        assert Service.objects.count() == 1
        specialist_user.delete()
        assert Service.objects.count() == 0

    def test_service_with_category(self, specialist_user):
        cat = ServiceCategory.objects.create(name='Маникюр тест')
        service = Service.objects.create(
            specialist=specialist_user.specialist_profile, name='Классический маникюр',
            price=Decimal('1500'), duration_minutes=60,
            category=cat,
        )
        assert service.category == cat
        assert cat.services.count() == 1

    def test_is_active_default(self, specialist_user):
        service = Service.objects.create(
            specialist=specialist_user.specialist_profile, name='Active test',
            price=Decimal('500'), duration_minutes=30,
        )
        assert service.is_active is True

    def test_category_set_null(self, specialist_user):
        cat = ServiceCategory.objects.create(name='Temp')
        service = Service.objects.create(
            specialist=specialist_user.specialist_profile, name='Test',
            price=Decimal('500'), duration_minutes=30,
            category=cat,
        )
        cat.delete()
        service.refresh_from_db()
        assert service.category is None


@pytest.mark.django_db
class TestServiceCategoryModel:
    def test_create_category(self):
        cat = ServiceCategory.objects.create(name='Маникюр')
        assert cat.slug == 'маникюр'
        assert str(cat) == 'Маникюр'

    def test_auto_slug(self):
        cat = ServiceCategory.objects.create(name='Наращивание ресниц')
        assert cat.slug  # slug generated

    def test_hierarchy(self):
        parent = ServiceCategory.objects.create(name='Ногти', sort_order=1)
        child = ServiceCategory.objects.create(
            name='Маникюр', parent=parent, sort_order=1,
        )
        assert child.parent == parent
        assert str(child) == 'Ногти → Маникюр'
        assert parent.children.count() == 1

    def test_cascade_delete_children(self):
        parent = ServiceCategory.objects.create(name='Волосы')
        ServiceCategory.objects.create(name='Стрижка', parent=parent)
        ServiceCategory.objects.create(name='Окрашивание', parent=parent)
        assert ServiceCategory.objects.count() == 3
        parent.delete()
        assert ServiceCategory.objects.count() == 0

    def test_category_cannot_be_own_parent(self):
        cat = ServiceCategory.objects.create(name='Self Loop')
        cat.parent = cat
        with pytest.raises(ValidationError):
            cat.save()

    def test_indirect_cycle_prevented(self):
        parent = ServiceCategory.objects.create(name='Parent')
        child = ServiceCategory.objects.create(
            name='Child', parent=parent,
        )
        parent.parent = child
        with pytest.raises(ValidationError):
            parent.save()

    def test_max_depth_exceeded(self):
        """Cannot create a grandchild category (depth > 2)."""
        root = ServiceCategory.objects.create(name='Root depth test')
        child = ServiceCategory.objects.create(name='Child depth test', parent=root)
        grandchild = ServiceCategory(name='Grandchild depth test', parent=child)
        with pytest.raises(ValidationError):
            grandchild.save()
