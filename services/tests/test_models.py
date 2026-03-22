import logging
from decimal import Decimal

import pytest

from services.models import Service, ServiceCategory

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestServiceModel:
    def test_create_service(self, specialist_user):
        service = Service.objects.create(
            specialist=specialist_user,
            name='Стрижка',
            price=Decimal('500.00'),
            duration_minutes=60,
        )
        logger.info("Created service: %s", service)
        assert str(service) == f'Стрижка — {specialist_user.username}'
        assert service.created_at is not None

    def test_cascade_delete(self, specialist_user):
        Service.objects.create(
            specialist=specialist_user, name='Test',
            price=Decimal('100'), duration_minutes=30,
        )
        assert Service.objects.count() == 1
        specialist_user.delete()
        assert Service.objects.count() == 0


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
