import logging
from decimal import Decimal

import pytest

from services.models import Service

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
