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
class TestServicePublicViewSet:
    """Tests for GET /api/v1/services/search/ — public read API."""
    URL = '/api/v1/services/search/'

    def test_list_active_services(
        self, authenticated_client, specialist_user,
    ):
        """Client sees only active services."""
        Service.objects.create(
            specialist=specialist_user, name='Active',
            price='1000', duration_minutes=60, is_active=True,
        )
        Service.objects.create(
            specialist=specialist_user, name='Hidden',
            price='500', duration_minutes=30, is_active=False,
        )
        response = authenticated_client.get(self.URL)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]['name'] == 'Active'

    def test_includes_specialist_info(
        self, authenticated_client, specialist_user,
    ):
        """Response includes specialist name, rating, reviews_count."""
        Service.objects.create(
            specialist=specialist_user, name='Маникюр',
            price='1500', duration_minutes=60,
        )
        response = authenticated_client.get(self.URL)
        assert response.status_code == status.HTTP_200_OK
        info = response.data[0]['specialist_info']
        assert info['display_name'] == 'Елена Мастер'
        assert float(info['rating']) == 4.8
        assert info['reviews_count'] == 25

    def test_detail_includes_address(
        self, authenticated_client, specialist_user,
    ):
        """Detail view includes specialist address and location."""
        svc = Service.objects.create(
            specialist=specialist_user, name='Стрижка',
            price='800', duration_minutes=45,
        )
        response = authenticated_client.get(f'{self.URL}{svc.pk}/')
        assert response.status_code == status.HTTP_200_OK
        assert response.data['specialist_address'] == 'Казань, ул. Баумана 1'
        assert response.data['specialist_location_lat'] is not None

    def test_filter_by_category(
        self, authenticated_client, specialist_user,
    ):
        """Can filter services by category."""
        cat = ServiceCategory.objects.create(name='Ногти фильтр')
        Service.objects.create(
            specialist=specialist_user, name='Маникюр',
            price='1500', duration_minutes=60, category=cat,
        )
        Service.objects.create(
            specialist=specialist_user, name='Стрижка',
            price='800', duration_minutes=45,
        )
        response = authenticated_client.get(
            self.URL, {'category': cat.pk},
        )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]['name'] == 'Маникюр'

    def test_filter_by_price_range(
        self, authenticated_client, specialist_user,
    ):
        """Can filter by min/max price."""
        Service.objects.create(
            specialist=specialist_user, name='Cheap',
            price='500', duration_minutes=30,
        )
        Service.objects.create(
            specialist=specialist_user, name='Expensive',
            price='3000', duration_minutes=90,
        )
        response = authenticated_client.get(
            self.URL, {'min_price': 1000, 'max_price': 5000},
        )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert response.data[0]['name'] == 'Expensive'

    def test_ordering_by_price(
        self, authenticated_client, specialist_user,
    ):
        """Can order by price ascending."""
        Service.objects.create(
            specialist=specialist_user, name='B',
            price='2000', duration_minutes=60,
        )
        Service.objects.create(
            specialist=specialist_user, name='A',
            price='500', duration_minutes=30,
        )
        response = authenticated_client.get(
            self.URL, {'ordering': 'price'},
        )
        assert response.status_code == status.HTTP_200_OK
        assert response.data[0]['name'] == 'A'
        assert response.data[1]['name'] == 'B'

    def test_unauthenticated_denied(self, client_api_client):
        """Unauthenticated user cannot access public search."""
        response = client_api_client.get(self.URL)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    def test_search_by_name(
        self, authenticated_client, specialist_user,
    ):
        """Can search services by name substring."""
        Service.objects.create(
            specialist=specialist_user, name='Классический маникюр',
            price='1500', duration_minutes=60,
        )
        Service.objects.create(
            specialist=specialist_user, name='Стрижка',
            price='800', duration_minutes=45,
        )
        response = authenticated_client.get(
            self.URL, {'name': 'маникюр'},
        )
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 1
        assert 'маникюр' in response.data[0]['name'].lower()

    def test_readonly_no_post(self, authenticated_client):
        """Public API is read-only — POST not allowed."""
        response = authenticated_client.post(
            self.URL,
            {'name': 'Hack', 'price': '100', 'duration_minutes': 30},
        )
        assert response.status_code == status.HTTP_405_METHOD_NOT_ALLOWED


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


@pytest.mark.django_db
class TestCategoriesAPI:
    """Tests for GET /api/v1/categories/ — standalone endpoint."""
    URL = '/api/v1/categories/'

    def test_list_root_categories(self):
        ServiceCategory.objects.create(name='Ногти', sort_order=1)
        ServiceCategory.objects.create(name='Волосы', sort_order=2)
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(self.URL)
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 2

    def test_filter_by_parent_id(self):
        parent = ServiceCategory.objects.create(name='Ногти', sort_order=1)
        ServiceCategory.objects.create(
            name='Маникюр', parent=parent, sort_order=1,
        )
        ServiceCategory.objects.create(
            name='Педикюр', parent=parent, sort_order=2,
        )
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        # Root only (default)
        response = client.get(self.URL)
        assert len(response.data) == 1
        # Children of parent
        response = client.get(self.URL, {'parent_id': parent.pk})
        assert response.status_code == status.HTTP_200_OK
        assert len(response.data) == 2
        names = [c['name'] for c in response.data]
        assert 'Маникюр' in names
        assert 'Педикюр' in names

    def test_specialists_count(self, specialist_user):
        cat = ServiceCategory.objects.create(name='Ногти count')
        Service.objects.create(
            specialist=specialist_user, name='Маникюр',
            price='1500', duration_minutes=60, category=cat,
        )
        Service.objects.create(
            specialist=specialist_user, name='Педикюр',
            price='1000', duration_minutes=45, category=cat,
        )
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(f'{self.URL}{cat.pk}/')
        assert response.status_code == status.HTTP_200_OK
        # 1 specialist with 2 services = specialists_count=1
        assert response.data['specialists_count'] == 1

    def test_has_specialists_count_field(self):
        ServiceCategory.objects.create(name='Empty cat')
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(self.URL)
        assert response.status_code == status.HTTP_200_OK
        assert 'specialists_count' in response.data[0]
        assert response.data[0]['specialists_count'] == 0

    def test_detail_view(self):
        cat = ServiceCategory.objects.create(name='Detailable')
        client = APIClient()
        client.defaults['HTTP_X_APP_TYPE'] = 'client'
        response = client.get(f'{self.URL}{cat.pk}/')
        assert response.status_code == status.HTTP_200_OK
        assert response.data['name'] == 'Detailable'
