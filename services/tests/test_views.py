import logging

import pytest
from rest_framework import status
from rest_framework.test import APIClient

from services.models import Service, ServiceCategory
from users.models import User

logger = logging.getLogger(__name__)


@pytest.mark.django_db
class TestServiceViewSet:
    def test_specialist_can_create(self, authenticated_specialist):
        data = {
            'name': 'Консультация',
            'description': 'Описание',
            'price': '1000.00',
            'duration_minutes': 60,
        }
        logger.info("POST /api/v1/services/ — specialist creates service")
        response = authenticated_specialist.post(
            '/api/v1/services/', data,
        )
        logger.info("Response %s: %s", response.status_code, response.data)
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['name'] == 'Консультация'

    def test_client_cannot_create(self, pro_api_client):
        user = User.objects.create_user(
            username='svc_client', password='pass', role='client',
            phone='+79990100002',
        )
        pro_api_client.force_authenticate(user=user)
        response = pro_api_client.post(
            '/api/v1/services/',
            {'name': 'Test', 'price': '100', 'duration_minutes': 30},
        )
        logger.info("Client tries to create service → %s", response.status_code)
        assert response.status_code == status.HTTP_403_FORBIDDEN

    def test_unauthenticated_denied(self, pro_api_client):
        response = pro_api_client.get('/api/v1/services/')
        logger.info("Unauthenticated → %s", response.status_code)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_specialist_sees_own_services(
        self, authenticated_specialist, specialist_user,
    ):
        Service.objects.create(
            specialist=specialist_user, name='Mine',
            price='500', duration_minutes=30,
        )
        other = User.objects.create_user(
            username='other_svc_spec', password='pass',
            role='specialist', phone='+79990100099',
        )
        Service.objects.create(
            specialist=other, name='NotMine',
            price='500', duration_minutes=30,
        )
        response = authenticated_specialist.get('/api/v1/services/')
        logger.info("Specialist sees %d services", len(response.data))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]['name'] == 'Mine'

    def test_create_service_with_category(
        self, authenticated_specialist,
    ):
        cat = ServiceCategory.objects.create(name='Ногти API')
        response = authenticated_specialist.post(
            '/api/v1/services/',
            {
                'name': 'Маникюр',
                'price': '1500.00',
                'duration_minutes': 60,
                'category': cat.pk,
            },
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['category'] == cat.pk
        assert response.data['category_name'] == 'Ногти API'

    def test_create_service_without_category(
        self, authenticated_specialist,
    ):
        response = authenticated_specialist.post(
            '/api/v1/services/',
            {
                'name': 'Без категории',
                'price': '500.00',
                'duration_minutes': 30,
            },
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['category'] is None

    def test_service_is_active_default(
        self, authenticated_specialist,
    ):
        response = authenticated_specialist.post(
            '/api/v1/services/',
            {'name': 'Active', 'price': '100', 'duration_minutes': 30},
        )
        assert response.status_code == status.HTTP_201_CREATED
        assert response.data['is_active'] is True

    def test_deactivate_service(
        self, authenticated_specialist, specialist_user,
    ):
        svc = Service.objects.create(
            specialist=specialist_user, name='ToHide',
            price='500', duration_minutes=30,
        )
        response = authenticated_specialist.patch(
            f'/api/v1/services/{svc.pk}/',
            {'is_active': False},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data['is_active'] is False


@pytest.mark.django_db
class TestServiceCategoryAPI:
    URL = '/api/v1/services/categories/'

    def test_list_categories_public(self):
        """Categories endpoint is public (no auth required)."""
        ServiceCategory.objects.create(name='Ногти', sort_order=1)
        ServiceCategory.objects.create(name='Волосы', sort_order=2)
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(self.URL)
        logger.info("GET categories → %s, count=%d",
                    response.status_code, len(response.data))
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 2

    def test_categories_with_children(self):
        parent = ServiceCategory.objects.create(name='Ногти', sort_order=1)
        ServiceCategory.objects.create(
            name='Маникюр', parent=parent, sort_order=1,
        )
        ServiceCategory.objects.create(
            name='Педикюр', parent=parent, sort_order=2,
        )
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(self.URL)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1  # only root
        assert len(response.data[0]['children']) == 2

    def test_inactive_categories_hidden(self):
        ServiceCategory.objects.create(
            name='Активная', sort_order=1, is_active=True,
        )
        ServiceCategory.objects.create(
            name='Скрытая', sort_order=2, is_active=False,
        )
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(self.URL)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]['name'] == 'Активная'

    def test_categories_from_fixture(self):
        """Verify fixture loaded correctly."""
        from django.core.management import call_command
        call_command('loaddata', 'categories', verbosity=0)
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(self.URL)
        assert response.status_code == status.HTTP_200_OK
        # 7 root categories from fixture
        root_names = [c['name'] for c in response.data]
        assert 'Ногтевой сервис' in root_names
        assert 'Волосы' in root_names
        assert 'Массаж' in root_names
